from app.config import settings
from app.providers.base import Provider
from app.providers.mock import MockProvider


def get_provider() -> Provider:
    mode = settings.provider_mode.lower().strip()
    # Step 1: mock only. Bedrock/Vertex land in Step 2.
    if mode in {"mock", "bedrock", "vertex", "multi"}:
        return MockProvider(latency_ms=settings.mock_latency_ms)
    raise ValueError(f"Unsupported PROVIDER_MODE: {settings.provider_mode}")
