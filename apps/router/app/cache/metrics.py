from __future__ import annotations

from dataclasses import dataclass

from app.metrics import record_cache_hit, record_cache_miss


@dataclass
class CacheMetrics:
    """Process-local counters for /v1/cache/stats (+ Prometheus side effects)."""

    cache_hit_total: int = 0
    cache_miss_total: int = 0
    estimated_usd_saved: float = 0.0

    def record_hit(self, saved_usd: float) -> None:
        saved = max(0.0, saved_usd)
        self.cache_hit_total += 1
        self.estimated_usd_saved += saved
        record_cache_hit(saved)

    def record_miss(self) -> None:
        self.cache_miss_total += 1
        record_cache_miss()

    @property
    def hit_rate(self) -> float:
        total = self.cache_hit_total + self.cache_miss_total
        if total == 0:
            return 0.0
        return self.cache_hit_total / total

    def as_dict(self) -> dict[str, float | int]:
        return {
            "cache_hit_total": self.cache_hit_total,
            "cache_miss_total": self.cache_miss_total,
            "estimated_usd_saved": round(self.estimated_usd_saved, 8),
            "hit_rate": round(self.hit_rate, 4),
        }

    def reset(self) -> None:
        self.cache_hit_total = 0
        self.cache_miss_total = 0
        self.estimated_usd_saved = 0.0


cache_metrics = CacheMetrics()
