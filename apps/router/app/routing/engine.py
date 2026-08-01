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
from app.metrics import observe_provider, record_provider_error, record_route_decision
from app.models import ChatCompletionRequest, ChatCompletionResponse
from app.providers.base import Provider
from app.providers.circuit_breaker import CircuitBreaker
from app.routing.policies import ordered_candidates
from app.streaming import new_completion_id

logger = logging.getLogger(__name__)


@dataclass
class RouteDecision:
    response: ChatCompletionResponse
    reason: str
    provider: str
    attempts: list[dict[str, str]]


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

    async def aclose(self) -> None:
        agen = self._agen
        if agen is not None and hasattr(agen, "aclose"):
            await agen.aclose()


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

    @property
    def providers(self) -> dict[str, Provider]:
        return self._providers

    async def complete(self, request: ChatCompletionRequest) -> RouteDecision:
        candidates, reason = ordered_candidates(request, self._providers, self._settings)
        attempts: list[dict[str, str]] = []

        if not candidates:
            raise AllProvidersFailedError([])

        timeout_s = self._settings.provider_timeout_ms / 1000.0

        for provider in candidates:
            breaker = self._breakers[provider.name]
            if not breaker.allow():
                attempts.append(
                    {
                        "provider": provider.name,
                        "error": f"circuit_open:{breaker.state.value}",
                    }
                )
                logger.warning(
                    "route skip provider=%s reason=circuit_open state=%s",
                    provider.name,
                    breaker.state.value,
                )
                continue

            started = time.perf_counter()
            try:
                response = await asyncio.wait_for(provider.complete(request), timeout=timeout_s)
            except TimeoutError as exc:
                breaker.record_failure()
                err = ProviderTimeoutError(provider.name, self._settings.provider_timeout_ms)
                attempts.append({"provider": provider.name, "error": str(err)})
                record_provider_error(provider.name, "timeout")
                observe_provider(
                    provider=provider.name,
                    outcome="error",
                    seconds=time.perf_counter() - started,
                )
                logger.warning(
                    "route fail provider=%s error=timeout timeout_ms=%s",
                    provider.name,
                    self._settings.provider_timeout_ms,
                )
                # Keep going — failover
                _ = exc
                continue
            except ProviderError as exc:
                breaker.record_failure()
                attempts.append({"provider": provider.name, "error": str(exc)})
                record_provider_error(provider.name, type(exc).__name__)
                observe_provider(
                    provider=provider.name,
                    outcome="error",
                    seconds=time.perf_counter() - started,
                )
                logger.warning(
                    "route fail provider=%s error=%s retryable=%s",
                    provider.name,
                    exc,
                    exc.retryable,
                )
                if not exc.retryable:
                    # Non-retryable still fails over so multi-cloud stays available.
                    continue
                continue
            except Exception as exc:
                breaker.record_failure()
                attempts.append({"provider": provider.name, "error": str(exc)})
                record_provider_error(provider.name, type(exc).__name__)
                observe_provider(
                    provider=provider.name,
                    outcome="error",
                    seconds=time.perf_counter() - started,
                )
                logger.exception("route fail provider=%s unexpected error", provider.name)
                continue

            breaker.record_success()
            observe_provider(
                provider=provider.name,
                outcome="ok",
                seconds=time.perf_counter() - started,
            )
            response.provider = provider.name
            response.route_reason = reason
            record_route_decision(provider.name, reason)
            logger.info(
                "route decision provider=%s reason=%s model=%s attempts=%s",
                provider.name,
                reason,
                request.model,
                len(attempts) + 1,
            )
            return RouteDecision(
                response=response,
                reason=reason,
                provider=provider.name,
                attempts=attempts,
            )

        raise AllProvidersFailedError(attempts)

    async def open_stream(self, request: ChatCompletionRequest) -> StreamRoute:
        """Pick a provider and open its text-delta stream (failover before first byte)."""
        candidates, reason = ordered_candidates(request, self._providers, self._settings)
        attempts: list[dict[str, str]] = []

        if not candidates:
            raise AllProvidersFailedError([])

        timeout_s = self._settings.provider_timeout_ms / 1000.0

        for provider in candidates:
            breaker = self._breakers[provider.name]
            if not breaker.allow():
                attempts.append(
                    {
                        "provider": provider.name,
                        "error": f"circuit_open:{breaker.state.value}",
                    }
                )
                logger.warning(
                    "route skip provider=%s reason=circuit_open state=%s stream=1",
                    provider.name,
                    breaker.state.value,
                )
                continue

            agen = provider.stream(request)
            started = time.perf_counter()
            try:
                first = await asyncio.wait_for(agen.__anext__(), timeout=timeout_s)
            except StopAsyncIteration:
                breaker.record_failure()
                attempts.append(
                    {"provider": provider.name, "error": "empty_stream"},
                )
                record_provider_error(provider.name, "empty_stream")
                observe_provider(
                    provider=provider.name,
                    outcome="error",
                    seconds=time.perf_counter() - started,
                )
                logger.warning("route fail provider=%s error=empty_stream", provider.name)
                continue
            except TimeoutError:
                await _aclose_quiet(agen)
                breaker.record_failure()
                err = ProviderTimeoutError(provider.name, self._settings.provider_timeout_ms)
                attempts.append({"provider": provider.name, "error": str(err)})
                record_provider_error(provider.name, "timeout")
                observe_provider(
                    provider=provider.name,
                    outcome="error",
                    seconds=time.perf_counter() - started,
                )
                logger.warning(
                    "route fail provider=%s error=timeout timeout_ms=%s stream=1",
                    provider.name,
                    self._settings.provider_timeout_ms,
                )
                continue
            except NotImplementedError as exc:
                await _aclose_quiet(agen)
                breaker.record_failure()
                attempts.append({"provider": provider.name, "error": str(exc)})
                record_provider_error(provider.name, "not_implemented")
                observe_provider(
                    provider=provider.name,
                    outcome="error",
                    seconds=time.perf_counter() - started,
                )
                logger.warning("route fail provider=%s error=%s stream=1", provider.name, exc)
                continue
            except ProviderError as exc:
                await _aclose_quiet(agen)
                breaker.record_failure()
                attempts.append({"provider": provider.name, "error": str(exc)})
                record_provider_error(provider.name, type(exc).__name__)
                observe_provider(
                    provider=provider.name,
                    outcome="error",
                    seconds=time.perf_counter() - started,
                )
                logger.warning(
                    "route fail provider=%s error=%s retryable=%s stream=1",
                    provider.name,
                    exc,
                    exc.retryable,
                )
                continue
            except Exception as exc:
                await _aclose_quiet(agen)
                breaker.record_failure()
                attempts.append({"provider": provider.name, "error": str(exc)})
                record_provider_error(provider.name, type(exc).__name__)
                observe_provider(
                    provider=provider.name,
                    outcome="error",
                    seconds=time.perf_counter() - started,
                )
                logger.exception(
                    "route fail provider=%s unexpected error stream=1", provider.name
                )
                continue

            breaker.record_success()
            observe_provider(
                provider=provider.name,
                outcome="ok",
                seconds=time.perf_counter() - started,
            )
            record_route_decision(provider.name, reason)
            logger.info(
                "route decision provider=%s reason=%s model=%s attempts=%s stream=1",
                provider.name,
                reason,
                request.model,
                len(attempts) + 1,
            )

            async def _deltas(
                first_delta: str = first,
                generator: AsyncIterator[str] = agen,
            ) -> AsyncIterator[str]:
                yield first_delta
                async for delta in generator:
                    yield delta

            deltas = _deltas()
            return StreamRoute(
                provider=provider.name,
                reason=reason,
                attempts=attempts,
                completion_id=new_completion_id(provider.name),
                created=int(time.time()),
                model=request.model,
                deltas=deltas,
                _agen=agen,
            )

        raise AllProvidersFailedError(attempts)

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
