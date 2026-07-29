"""Structured provider / routing errors mapped to HTTP responses."""

from __future__ import annotations


class ProviderError(Exception):
    """Base error raised by a provider adapter."""

    def __init__(self, message: str, *, provider: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


class ProviderTimeoutError(ProviderError):
    def __init__(self, provider: str, timeout_ms: int) -> None:
        super().__init__(
            f"Provider '{provider}' timed out after {timeout_ms}ms",
            provider=provider,
            retryable=True,
        )
        self.timeout_ms = timeout_ms


class ProviderUnavailableError(ProviderError):
    def __init__(self, provider: str, reason: str = "unavailable") -> None:
        super().__init__(
            f"Provider '{provider}' is {reason}",
            provider=provider,
            retryable=True,
        )


class AllProvidersFailedError(Exception):
    """Every candidate provider failed or was skipped by the circuit breaker."""

    def __init__(self, attempts: list[dict[str, str]]) -> None:
        self.attempts = attempts
        detail = "; ".join(f"{a['provider']}: {a['error']}" for a in attempts) or "none"
        super().__init__(f"All providers failed ({detail})")
