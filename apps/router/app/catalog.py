"""OpenAI-shaped model catalog (logical chat models + local embedding aliases)."""

from __future__ import annotations

from app.config import Settings
from app.config import settings as default_settings
from app.models import ModelCard

# Stable created timestamp so catalog payloads are deterministic in tests / OpenAPI.
_CATALOG_CREATED = 1_700_000_000

# SDK-friendly aliases that all resolve to the configured local embedder
# (hashing by default; sentence-transformers when CACHE_EMBEDDING_PROVIDER is set).
EMBEDDING_MODEL_IDS: tuple[str, ...] = (
    "text-embedding-hashing",
    "text-embedding-3-small",
)

_EMBEDDING_OWNED_BY = "local"


def _embedding_cards() -> list[ModelCard]:
    return [
        ModelCard(
            id=model_id,
            created=_CATALOG_CREATED,
            owned_by=_EMBEDDING_OWNED_BY,
            purpose="embeddings",
        )
        for model_id in EMBEDDING_MODEL_IDS
    ]


def _chat_cards(cfg: Settings) -> list[ModelCard]:
    cards: list[ModelCard] = []
    seen: set[str] = set()
    for logical, providers in cfg.parsed_model_map.items():
        if logical in seen:
            continue
        seen.add(logical)
        owned = ",".join(providers.keys()) if providers else "gateway"
        cards.append(
            ModelCard(
                id=logical,
                created=_CATALOG_CREATED,
                owned_by=owned,
                purpose="chat",
            )
        )
    return cards


def list_models(settings: Settings | None = None) -> list[ModelCard]:
    cfg = settings or default_settings
    return [*_chat_cards(cfg), *_embedding_cards()]


def get_model(model_id: str, settings: Settings | None = None) -> ModelCard | None:
    wanted = model_id.strip()
    if not wanted:
        return None
    for card in list_models(settings):
        if card.id == wanted:
            return card
    return None


def is_embedding_model(model_id: str) -> bool:
    return model_id.strip() in EMBEDDING_MODEL_IDS
