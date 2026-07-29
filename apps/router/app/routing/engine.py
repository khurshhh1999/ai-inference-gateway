from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.config import Settings, settings as default_settings
from app.errors import (
    AllProvidersFailedError,
    ProviderError,
    ProviderTimeoutError,
)
from app.models import ChatCompletionRequest, ChatCompletionResponse
from app.providers.base import Provider
from app.providers.circuit_breaker import CircuitBreaker
from app.routing.policies import ordered_candidates

logger = logging.getLogger(__name__)


@dataclass
class RouteDecision:
    response: ChatCompletionResponse
    reason: str
    provider: str
    attempts: list[dict[str, str]]


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

            try:
                response = await asyncio.wait_for(provider.complete(request), timeout=timeout_s)
            except TimeoutError as exc:
                breaker.record_failure()
                err = ProviderTimeoutError(provider.name, self._settings.provider_timeout_ms)
                attempts.append({"provider": provider.name, "error": str(err)})
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
            except Exception as exc:  # noqa: BLE001
                breaker.record_failure()
                attempts.append({"provider": provider.name, "error": str(exc)})
                logger.exception("route fail provider=%s unexpected error", provider.name)
                continue

            breaker.record_success()
            response.provider = provider.name
            response.route_reason = reason
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

    async def health(self) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for name, provider in self._providers.items():
            try:
                results[name] = await provider.health()
            except Exception:  # noqa: BLE001
                results[name] = False
        return results
