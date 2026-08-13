from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.budget.meter import BudgetExceededError, get_budget_meter
from app.budget.pricing import billable_cost_usd
from app.cache.embeddings import resize_embedding
from app.cache.semantic import get_semantic_cache
from app.catalog import is_embedding_model
from app.config import settings
from app.metrics import observe_request, record_budget_rejection, record_spend
from app.models import EmbeddingData, EmbeddingRequest, EmbeddingResponse, EmbeddingUsage
from app.tracing import get_tracer, set_span_error, set_span_ok

logger = logging.getLogger(__name__)
router = APIRouter()
tracer = get_tracer("router.embeddings")

_MAX_INPUTS = 128


def _budget_exceeded_http(exc: BudgetExceededError) -> HTTPException:
    record_budget_rejection(exc.window, exc.metric)
    return HTTPException(
        status_code=exc.status_code,
        detail={
            "error": "budget_exceeded",
            "message": str(exc),
            "tenant": exc.tenant,
            "window": exc.window,
            "metric": exc.metric,
            "used": exc.used,
            "limit": exc.limit,
        },
    )


def _texts(body: EmbeddingRequest) -> list[str]:
    if isinstance(body.input, str):
        return [body.input]
    return list(body.input)


def _token_count(texts: list[str]) -> int:
    return max(1, sum(max(1, len(text.split())) if text.strip() else 1 for text in texts))


@router.post("/v1/embeddings", response_model=None)
async def create_embeddings(
    body: EmbeddingRequest,
    request: Request,
    response: Response,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> EmbeddingResponse | JSONResponse:
    started = time.perf_counter()
    tenant = (x_tenant_id or "default").strip() or "default"
    request_id = getattr(request.state, "request_id", None) or "unknown"
    status = 200

    with tracer.start_as_current_span(
        "router.embeddings",
        attributes={
            "http.request_id": request_id,
            "tenant.id": tenant,
            "llm.model": body.model,
            "http.route": "/v1/embeddings",
        },
    ) as span:
        try:
            if not is_embedding_model(body.model):
                status = 400
                span.set_attribute("error", True)
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "message": (
                                f"Model '{body.model}' is not an embedding model. "
                                "Use text-embedding-3-small or text-embedding-hashing."
                            ),
                            "type": "invalid_request_error",
                            "code": "invalid_model",
                        }
                    },
                )

            texts = _texts(body)
            if len(texts) > _MAX_INPUTS:
                status = 400
                span.set_attribute("error", True)
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "message": f"input list exceeds max of {_MAX_INPUTS} items",
                            "type": "invalid_request_error",
                        }
                    },
                )

            prompt_tokens = _token_count(texts)
            usd = billable_cost_usd(
                provider="embeddings",
                model=body.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                settings=settings,
            )

            meter = get_budget_meter()
            try:
                check = await meter.check(
                    tenant,
                    estimated_usd=usd,
                    estimated_tokens=prompt_tokens,
                )
            except BudgetExceededError as exc:
                status = exc.status_code
                set_span_error(span, exc)
                raise _budget_exceeded_http(exc) from exc
            if check.soft_warning:
                response.headers["X-Budget-Warning"] = "soft"

            cache = get_semantic_cache()
            data: list[EmbeddingData] = []
            for index, text in enumerate(texts):
                vec = cache.embed(text)
                if body.dimensions is not None:
                    try:
                        vec = resize_embedding(vec, body.dimensions)
                    except ValueError as exc:
                        status = 400
                        set_span_error(span, exc)
                        return JSONResponse(
                            status_code=400,
                            content={
                                "error": {
                                    "message": str(exc),
                                    "type": "invalid_request_error",
                                }
                            },
                        )
                data.append(EmbeddingData(embedding=vec, index=index))
            try:
                await meter.record(
                    tenant=tenant,
                    usd=usd,
                    tokens=prompt_tokens,
                    provider="embeddings",
                    model=body.model,
                )
                record_spend("embeddings", usd)
            except Exception:
                logger.exception(
                    "budget record failed tenant=%s model=%s request_id=%s",
                    tenant,
                    body.model,
                    request_id,
                )

            dim = len(data[0].embedding) if data else cache.embedding_dim
            span.set_attribute("embedding.dim", dim)
            span.set_attribute("embedding.count", len(data))
            set_span_ok(span)
            return EmbeddingResponse(
                data=data,
                model=body.model,
                usage=EmbeddingUsage(
                    prompt_tokens=prompt_tokens,
                    total_tokens=prompt_tokens,
                ),
                embedding_provider=settings.cache_embedding_provider,
                dim=dim,
            )
        finally:
            observe_request(
                method="POST",
                route="/v1/embeddings",
                status=status,
                cached=False,
                stream=False,
                seconds=time.perf_counter() - started,
            )
