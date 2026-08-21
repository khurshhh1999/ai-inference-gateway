from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.config import Settings
from app.config import settings as default_settings
from app.errors import (
    AllProvidersFailedError,
    ProviderError,
    ProviderTimeoutError,
)
from app.metrics import (
    observe_provider,
    record_hedge_cancelled,
    record_hedge_fired,
    record_hedge_won,
    record_provider_error,
    record_route_decision,
    set_adaptive_gauges,
)
from app.models import ChatCompletionRequest, ChatCompletionResponse
from app.providers.base import Provider
from app.providers.circuit_breaker import CircuitBreaker
from app.routing.policies import ordered_candidates
from app.routing.signals import AdaptiveSignals
from app.streaming import new_completion_id
from app.tracing import get_tracer, set_span_error, set_span_ok

logger = logging.getLogger(__name__)
_tracer = get_tracer("router.routing")


@dataclass
class RouteDecision:
    response: ChatCompletionResponse
    reason: str
    provider: str
    attempts: list[dict[str, str]]
    hedged: bool = False


@dataclass
class StreamRoute:
    """Active streaming route: metadata + text-delta iterator from the chosen provider."""

    provider: str
    reason: str
    attempts: list[dict[str, str]]
    completion_id: str
    created: int
    model: str
    deltas: AsyncIterator[str]
    _agen: AsyncIterator[str] | None = field(default=None, repr=False)
    hedged: bool = False

    async def aclose(self) -> None:
        agen = self._agen
        if agen is not None and hasattr(agen, "aclose"):
            await agen.aclose()


@dataclass
class _AttemptResult:
    provider: str
    ok: bool
    error: str | None = None
    cancelled: bool = False
    response: ChatCompletionResponse | None = None
    first_delta: str | None = None
    agen: AsyncIterator[str] | None = None


class RoutingEngine:
    """Select a provider by policy, enforce timeout + circuit breaker, failover on error."""

    def __init__(
        self,
        providers: dict[str, Provider],
        *,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or default_settings
        self._providers = providers
        self._breakers = {
            name: CircuitBreaker(
                failure_threshold=self._settings.circuit_breaker_failure_threshold,
                reset_ms=self._settings.circuit_breaker_reset_ms,
            )
            for name in providers
        }
        self._signals = AdaptiveSignals(
            alpha=self._settings.adaptive_ewma_alpha,
            error_penalty_ms=self._settings.adaptive_error_penalty_ms,
            min_samples=self._settings.adaptive_min_samples,
            stale_after_seconds=self._settings.adaptive_stale_after_seconds,
            latency_hints_ms=self._settings.latency_hints_ms,
        )

    @property
    def providers(self) -> dict[str, Provider]:
        return self._providers

    @property
    def signals(self) -> AdaptiveSignals:
        return self._signals

    def signals_snapshot(self) -> dict[str, dict[str, float | int | bool]]:
        return self._signals.as_dict(list(self._providers))

    def _hedge_after_s(self) -> float:
        return max(0.0, self._settings.hedge_after_ms / 1000.0)

    def _observe_provider(self, name: str, started: float, outcome: str) -> None:
        elapsed = time.perf_counter() - started
        observe_provider(provider=name, outcome=outcome, seconds=elapsed)
        self._signals.observe(name, elapsed * 1000.0, error=(outcome != "ok"))
        snap = self._signals.snapshot([name])[name]
        set_adaptive_gauges(
            name,
            latency_ms=snap.ewma_latency_ms,
            error_rate=snap.ewma_error_rate,
            score_ms=snap.score,
            samples=snap.samples,
        )

    def _pop_allowed(
        self,
        remaining: list[Provider],
        attempts: list[dict[str, str]],
        *,
        stream: bool = False,
    ) -> Provider | None:
        while remaining:
            provider = remaining.pop(0)
            breaker = self._breakers[provider.name]
            if breaker.allow():
                return provider
            attempts.append(
                {
                    "provider": provider.name,
                    "error": f"circuit_open:{breaker.state.value}",
                }
            )
            logger.warning(
                "route skip provider=%s reason=circuit_open state=%s%s",
                provider.name,
                breaker.state.value,
                " stream=1" if stream else "",
            )
        return None

    async def complete(self, request: ChatCompletionRequest) -> RouteDecision:
        candidates, reason = ordered_candidates(
            request, self._providers, self._settings, signals=self._signals
        )
        if not candidates:
            raise AllProvidersFailedError([])
        if self._hedge_after_s() > 0:
            return await self._complete_hedged(request, candidates, reason)
        return await self._complete_sequential(request, candidates, reason)

    async def open_stream(self, request: ChatCompletionRequest) -> StreamRoute:
        """Pick a provider and open its text-delta stream (failover before first byte)."""
        candidates, reason = ordered_candidates(
            request, self._providers, self._settings, signals=self._signals
        )
        if not candidates:
            raise AllProvidersFailedError([])
        if self._hedge_after_s() > 0:
            return await self._stream_hedged(request, candidates, reason)
        return await self._stream_sequential(request, candidates, reason)

    async def _complete_sequential(
        self,
        request: ChatCompletionRequest,
        candidates: list[Provider],
        reason: str,
    ) -> RouteDecision:
        attempts: list[dict[str, str]] = []
        remaining = list(candidates)
        timeout_s = self._settings.provider_timeout_ms / 1000.0
        while True:
            provider = self._pop_allowed(remaining, attempts)
            if provider is None:
                break
            outcome = await self._complete_one(provider, request, timeout_s)
            if outcome.ok:
                return self._accept_complete(outcome, reason, attempts, hedged=False)
            attempts.append(
                {"provider": outcome.provider, "error": outcome.error or "error"}
            )
        raise AllProvidersFailedError(attempts)

    async def _stream_sequential(
        self,
        request: ChatCompletionRequest,
        candidates: list[Provider],
        reason: str,
    ) -> StreamRoute:
        attempts: list[dict[str, str]] = []
        remaining = list(candidates)
        timeout_s = self._settings.provider_timeout_ms / 1000.0
        while True:
            provider = self._pop_allowed(remaining, attempts, stream=True)
            if provider is None:
                break
            outcome = await self._stream_one(provider, request, timeout_s)
            if outcome.ok:
                return self._accept_stream(
                    outcome, reason, attempts, request, hedged=False
                )
            attempts.append(
                {"provider": outcome.provider, "error": outcome.error or "error"}
            )
        raise AllProvidersFailedError(attempts)

    async def _complete_hedged(
        self,
        request: ChatCompletionRequest,
        candidates: list[Provider],
        reason: str,
    ) -> RouteDecision:
        attempts: list[dict[str, str]] = []
        remaining = list(candidates)
        timeout_s = self._settings.provider_timeout_ms / 1000.0
        hedge_s = self._hedge_after_s()

        while remaining:
            primary = self._pop_allowed(remaining, attempts)
            if primary is None:
                break
            primary_task = asyncio.create_task(
                self._complete_one(primary, request, timeout_s),
                name=f"complete:{primary.name}",
            )
            done, _ = await asyncio.wait({primary_task}, timeout=hedge_s)
            if primary_task in done:
                outcome = _task_result(primary_task)
                if outcome.ok:
                    return self._accept_complete(outcome, reason, attempts, hedged=False)
                attempts.append(
                    {"provider": outcome.provider, "error": outcome.error or "error"}
                )
                continue

            secondary = self._pop_allowed(remaining, attempts)
            if secondary is None:
                outcome = await primary_task
                if outcome.ok:
                    return self._accept_complete(outcome, reason, attempts, hedged=False)
                attempts.append(
                    {"provider": outcome.provider, "error": outcome.error or "error"}
                )
                continue

            record_hedge_fired(primary.name, secondary.name)
            logger.info(
                "hedge fired primary=%s secondary=%s after_ms=%s",
                primary.name,
                secondary.name,
                self._settings.hedge_after_ms,
            )
            hedge_task = asyncio.create_task(
                self._complete_one(secondary, request, timeout_s),
                name=f"complete:{secondary.name}",
            )
            decision = await self._first_ok_complete(
                primary_task, hedge_task, reason, attempts
            )
            if decision is not None:
                return decision
        raise AllProvidersFailedError(attempts)

    async def _stream_hedged(
        self,
        request: ChatCompletionRequest,
        candidates: list[Provider],
        reason: str,
    ) -> StreamRoute:
        attempts: list[dict[str, str]] = []
        remaining = list(candidates)
        timeout_s = self._settings.provider_timeout_ms / 1000.0
        hedge_s = self._hedge_after_s()

        while remaining:
            primary = self._pop_allowed(remaining, attempts, stream=True)
            if primary is None:
                break
            primary_task = asyncio.create_task(
                self._stream_one(primary, request, timeout_s),
                name=f"stream:{primary.name}",
            )
            done, _ = await asyncio.wait({primary_task}, timeout=hedge_s)
            if primary_task in done:
                outcome = _task_result(primary_task)
                if outcome.ok:
                    return self._accept_stream(
                        outcome, reason, attempts, request, hedged=False
                    )
                attempts.append(
                    {"provider": outcome.provider, "error": outcome.error or "error"}
                )
                continue

            secondary = self._pop_allowed(remaining, attempts, stream=True)
            if secondary is None:
                outcome = await primary_task
                if outcome.ok:
                    return self._accept_stream(
                        outcome, reason, attempts, request, hedged=False
                    )
                attempts.append(
                    {"provider": outcome.provider, "error": outcome.error or "error"}
                )
                continue

            record_hedge_fired(primary.name, secondary.name)
            logger.info(
                "hedge fired primary=%s secondary=%s after_ms=%s stream=1",
                primary.name,
                secondary.name,
                self._settings.hedge_after_ms,
            )
            hedge_task = asyncio.create_task(
                self._stream_one(secondary, request, timeout_s),
                name=f"stream:{secondary.name}",
            )
            route = await self._first_ok_stream(
                primary_task, hedge_task, reason, attempts, request
            )
            if route is not None:
                return route
        raise AllProvidersFailedError(attempts)

    async def _first_ok_complete(
        self,
        primary_task: asyncio.Task[_AttemptResult],
        hedge_task: asyncio.Task[_AttemptResult],
        reason: str,
        attempts: list[dict[str, str]],
    ) -> RouteDecision | None:
        pending: set[asyncio.Task[_AttemptResult]] = {primary_task, hedge_task}
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task.cancelled():
                    continue
                outcome = task.result()
                if outcome.cancelled:
                    continue
                if outcome.ok:
                    for other in pending:
                        await self._cancel_call(other)
                    won_hedge = task is hedge_task
                    record_hedge_won(
                        outcome.provider, "secondary" if won_hedge else "primary"
                    )
                    return self._accept_complete(
                        outcome,
                        "hedged" if won_hedge else reason,
                        attempts,
                        hedged=won_hedge,
                    )
                attempts.append(
                    {"provider": outcome.provider, "error": outcome.error or "error"}
                )
        return None

    async def _first_ok_stream(
        self,
        primary_task: asyncio.Task[_AttemptResult],
        hedge_task: asyncio.Task[_AttemptResult],
        reason: str,
        attempts: list[dict[str, str]],
        request: ChatCompletionRequest,
    ) -> StreamRoute | None:
        pending: set[asyncio.Task[_AttemptResult]] = {primary_task, hedge_task}
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task.cancelled():
                    continue
                outcome = task.result()
                if outcome.cancelled:
                    continue
                if outcome.ok:
                    for other in pending:
                        await self._cancel_call(other)
                    won_hedge = task is hedge_task
                    record_hedge_won(
                        outcome.provider, "secondary" if won_hedge else "primary"
                    )
                    return self._accept_stream(
                        outcome,
                        "hedged" if won_hedge else reason,
                        attempts,
                        request,
                        hedged=won_hedge,
                    )
                attempts.append(
                    {"provider": outcome.provider, "error": outcome.error or "error"}
                )
        return None

    async def _cancel_call(self, task: asyncio.Task[_AttemptResult]) -> None:
        provider = "unknown"
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                name = task.get_name()
                if ":" in name:
                    provider = name.rsplit(":", 1)[-1]
                record_hedge_cancelled(provider)
                return
        if task.cancelled():
            name = task.get_name()
            if ":" in name:
                provider = name.rsplit(":", 1)[-1]
            record_hedge_cancelled(provider)
            return
        outcome = task.result()
        if outcome.cancelled:
            record_hedge_cancelled(outcome.provider)

    def _accept_complete(
        self,
        outcome: _AttemptResult,
        reason: str,
        attempts: list[dict[str, str]],
        *,
        hedged: bool,
    ) -> RouteDecision:
        response = outcome.response
        assert response is not None
        response.provider = outcome.provider
        response.route_reason = reason
        record_route_decision(outcome.provider, reason)
        logger.info(
            "route decision provider=%s reason=%s model=%s attempts=%s hedged=%s",
            outcome.provider,
            reason,
            response.model,
            len(attempts) + 1,
            int(hedged),
        )
        return RouteDecision(
            response=response,
            reason=reason,
            provider=outcome.provider,
            attempts=attempts,
            hedged=hedged,
        )

    def _accept_stream(
        self,
        outcome: _AttemptResult,
        reason: str,
        attempts: list[dict[str, str]],
        request: ChatCompletionRequest,
        *,
        hedged: bool,
    ) -> StreamRoute:
        agen = outcome.agen
        first = outcome.first_delta
        assert agen is not None and first is not None
        record_route_decision(outcome.provider, reason)
        logger.info(
            "route decision provider=%s reason=%s model=%s attempts=%s stream=1 hedged=%s",
            outcome.provider,
            reason,
            request.model,
            len(attempts) + 1,
            int(hedged),
        )

        async def _deltas(
            first_delta: str = first,
            generator: AsyncIterator[str] = agen,
        ) -> AsyncIterator[str]:
            yield first_delta
            async for delta in generator:
                yield delta

        return StreamRoute(
            provider=outcome.provider,
            reason=reason,
            attempts=attempts,
            completion_id=new_completion_id(outcome.provider),
            created=int(time.time()),
            model=request.model,
            deltas=_deltas(),
            _agen=agen,
            hedged=hedged,
        )

    async def _complete_one(
        self,
        provider: Provider,
        request: ChatCompletionRequest,
        timeout_s: float,
    ) -> _AttemptResult:
        breaker = self._breakers[provider.name]
        started = time.perf_counter()
        with _tracer.start_as_current_span(
            f"provider.{provider.name}.complete",
            attributes={
                "provider.name": provider.name,
                "llm.model": request.model,
            },
        ) as span:
            try:
                response = await asyncio.wait_for(
                    provider.complete(request), timeout=timeout_s
                )
            except asyncio.CancelledError:
                span.set_attribute("cancelled", True)
                raise
            except TimeoutError as exc:
                breaker.record_failure()
                err = ProviderTimeoutError(
                    provider.name, self._settings.provider_timeout_ms
                )
                record_provider_error(provider.name, "timeout")
                self._observe_provider(provider.name, started, "error")
                set_span_error(span, err)
                logger.warning(
                    "route fail provider=%s error=timeout timeout_ms=%s",
                    provider.name,
                    self._settings.provider_timeout_ms,
                )
                _ = exc
                return _AttemptResult(provider=provider.name, ok=False, error=str(err))
            except ProviderError as exc:
                breaker.record_failure()
                record_provider_error(provider.name, type(exc).__name__)
                self._observe_provider(provider.name, started, "error")
                set_span_error(span, exc)
                logger.warning(
                    "route fail provider=%s error=%s retryable=%s",
                    provider.name,
                    exc,
                    exc.retryable,
                )
                return _AttemptResult(provider=provider.name, ok=False, error=str(exc))
            except Exception as exc:
                breaker.record_failure()
                record_provider_error(provider.name, type(exc).__name__)
                self._observe_provider(provider.name, started, "error")
                set_span_error(span, exc)
                logger.exception("route fail provider=%s unexpected error", provider.name)
                return _AttemptResult(provider=provider.name, ok=False, error=str(exc))

            breaker.record_success()
            self._observe_provider(provider.name, started, "ok")
            set_span_ok(span)
            return _AttemptResult(provider=provider.name, ok=True, response=response)

    async def _stream_one(
        self,
        provider: Provider,
        request: ChatCompletionRequest,
        timeout_s: float,
    ) -> _AttemptResult:
        breaker = self._breakers[provider.name]
        agen = provider.stream(request)
        started = time.perf_counter()
        with _tracer.start_as_current_span(
            f"provider.{provider.name}.stream",
            attributes={
                "provider.name": provider.name,
                "llm.model": request.model,
                "llm.stream": True,
            },
        ) as span:
            try:
                first = await asyncio.wait_for(agen.__anext__(), timeout=timeout_s)
            except asyncio.CancelledError:
                await _aclose_quiet(agen)
                span.set_attribute("cancelled", True)
                raise
            except StopAsyncIteration:
                breaker.record_failure()
                record_provider_error(provider.name, "empty_stream")
                self._observe_provider(provider.name, started, "error")
                set_span_error(span, "empty_stream")
                logger.warning("route fail provider=%s error=empty_stream", provider.name)
                return _AttemptResult(
                    provider=provider.name, ok=False, error="empty_stream"
                )
            except TimeoutError:
                await _aclose_quiet(agen)
                breaker.record_failure()
                err = ProviderTimeoutError(
                    provider.name, self._settings.provider_timeout_ms
                )
                record_provider_error(provider.name, "timeout")
                self._observe_provider(provider.name, started, "error")
                set_span_error(span, err)
                logger.warning(
                    "route fail provider=%s error=timeout timeout_ms=%s stream=1",
                    provider.name,
                    self._settings.provider_timeout_ms,
                )
                return _AttemptResult(provider=provider.name, ok=False, error=str(err))
            except NotImplementedError as exc:
                await _aclose_quiet(agen)
                breaker.record_failure()
                record_provider_error(provider.name, "not_implemented")
                self._observe_provider(provider.name, started, "error")
                set_span_error(span, exc)
                logger.warning(
                    "route fail provider=%s error=%s stream=1", provider.name, exc
                )
                return _AttemptResult(provider=provider.name, ok=False, error=str(exc))
            except ProviderError as exc:
                await _aclose_quiet(agen)
                breaker.record_failure()
                record_provider_error(provider.name, type(exc).__name__)
                self._observe_provider(provider.name, started, "error")
                set_span_error(span, exc)
                logger.warning(
                    "route fail provider=%s error=%s retryable=%s stream=1",
                    provider.name,
                    exc,
                    exc.retryable,
                )
                return _AttemptResult(provider=provider.name, ok=False, error=str(exc))
            except Exception as exc:
                await _aclose_quiet(agen)
                breaker.record_failure()
                record_provider_error(provider.name, type(exc).__name__)
                self._observe_provider(provider.name, started, "error")
                set_span_error(span, exc)
                logger.exception(
                    "route fail provider=%s unexpected error stream=1", provider.name
                )
                return _AttemptResult(provider=provider.name, ok=False, error=str(exc))

            breaker.record_success()
            self._observe_provider(provider.name, started, "ok")
            set_span_ok(span)
            return _AttemptResult(
                provider=provider.name,
                ok=True,
                first_delta=first,
                agen=agen,
            )

    async def health(self) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for name, provider in self._providers.items():
            try:
                results[name] = await provider.health()
            except Exception:  # noqa: BLE001
                results[name] = False
        return results


async def _aclose_quiet(agen: AsyncIterator[str]) -> None:
    if hasattr(agen, "aclose"):
        try:
            await agen.aclose()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _task_result(task: asyncio.Task[_AttemptResult]) -> _AttemptResult:
    if task.cancelled():
        raise asyncio.CancelledError()
    return task.result()
