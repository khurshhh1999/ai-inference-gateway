from __future__ import annotations

import time
from dataclasses import dataclass

_DEFAULT_HINT_MS = 1_000.0


@dataclass(frozen=True)
class ProviderSnapshot:
    samples: int
    ewma_latency_ms: float
    ewma_error_rate: float
    score: float
    cold_start: bool
    stale: bool


class AdaptiveSignals:
    """Per-provider EWMA latency + error rate for adaptive routing.

    Cold start uses configured latency hints (same ranking as ``prefer_latency``).
    After live samples, score = EWMA latency (ms) + error_penalty_ms * EWMA error rate.
    Providers with no observations for ``stale_after_seconds`` fall back to hints so
    a recovered endpoint can earn traffic again without a config change.
    """

    def __init__(
        self,
        *,
        alpha: float = 0.3,
        error_penalty_ms: float = 1_000.0,
        min_samples: int = 1,
        stale_after_seconds: float = 30.0,
        latency_hints_ms: dict[str, float] | None = None,
    ) -> None:
        self.alpha = min(1.0, max(0.01, alpha))
        self.error_penalty_ms = max(0.0, error_penalty_ms)
        self.min_samples = max(1, min_samples)
        self.stale_after_seconds = max(0.0, stale_after_seconds)
        self._hints = dict(latency_hints_ms or {})
        self._latency_ms: dict[str, float] = {}
        self._error_rate: dict[str, float] = {}
        self._samples: dict[str, int] = {}
        self._last_ts: dict[str, float] = {}

    def observe(self, provider: str, latency_ms: float, *, error: bool) -> None:
        sample_lat = max(0.0, latency_ms)
        sample_err = 1.0 if error else 0.0
        reseed = provider not in self._latency_ms or self.is_stale(provider)
        if reseed:
            self._latency_ms[provider] = sample_lat
            self._error_rate[provider] = sample_err
            self._samples[provider] = 1
        else:
            a = self.alpha
            prev_lat = self._latency_ms[provider]
            prev_err = self._error_rate[provider]
            self._latency_ms[provider] = a * sample_lat + (1.0 - a) * prev_lat
            self._error_rate[provider] = a * sample_err + (1.0 - a) * prev_err
            self._samples[provider] = self._samples.get(provider, 0) + 1
        self._last_ts[provider] = time.monotonic()

    def is_stale(self, provider: str, *, now: float | None = None) -> bool:
        last = self._last_ts.get(provider)
        if last is None:
            return False
        if self.stale_after_seconds <= 0:
            return False
        clock = time.monotonic() if now is None else now
        return (clock - last) >= self.stale_after_seconds

    def is_cold(self, provider: str) -> bool:
        if self.is_stale(provider):
            return True
        return self._samples.get(provider, 0) < self.min_samples

    def latency_ms(self, provider: str) -> float:
        if self.is_cold(provider):
            return self._hints.get(provider, _DEFAULT_HINT_MS)
        return self._latency_ms.get(provider, self._hints.get(provider, _DEFAULT_HINT_MS))

    def error_rate(self, provider: str) -> float:
        if self.is_cold(provider):
            return 0.0
        return self._error_rate.get(provider, 0.0)

    def score(self, provider: str) -> float:
        """Lower is better. Unit: milliseconds-equivalent."""
        return self.latency_ms(provider) + self.error_penalty_ms * self.error_rate(provider)

    def snapshot(self, providers: list[str]) -> dict[str, ProviderSnapshot]:
        out: dict[str, ProviderSnapshot] = {}
        for name in providers:
            stale = self.is_stale(name)
            cold = self.is_cold(name)
            samples = 0 if stale else self._samples.get(name, 0)
            out[name] = ProviderSnapshot(
                samples=samples,
                ewma_latency_ms=self.latency_ms(name),
                ewma_error_rate=self.error_rate(name),
                score=self.score(name),
                cold_start=cold,
                stale=stale,
            )
        return out

    def as_dict(self, providers: list[str]) -> dict[str, dict[str, float | int | bool]]:
        snap = self.snapshot(providers)
        return {
            name: {
                "samples": s.samples,
                "ewma_latency_ms": round(s.ewma_latency_ms, 3),
                "ewma_error_rate": round(s.ewma_error_rate, 4),
                "score": round(s.score, 3),
                "cold_start": s.cold_start,
                "stale": s.stale,
            }
            for name, s in snap.items()
        }
