from __future__ import annotations

import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.catalog import get_model, list_models
from app.metrics import observe_request
from app.models import ModelCard, ModelListResponse

router = APIRouter()


@router.get("/v1/models")
async def list_available_models() -> ModelListResponse:
    started = time.perf_counter()
    try:
        return ModelListResponse(data=list_models())
    finally:
        observe_request(
            method="GET",
            route="/v1/models",
            status=200,
            cached=False,
            stream=False,
            seconds=time.perf_counter() - started,
        )


@router.get("/v1/models/{model_id:path}", response_model=None)
async def retrieve_model(model_id: str) -> ModelCard | JSONResponse:
    started = time.perf_counter()
    status = 200
    try:
        card = get_model(model_id)
        if card is None:
            status = 404
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "message": f"The model '{model_id}' does not exist",
                        "type": "invalid_request_error",
                        "code": "model_not_found",
                    }
                },
            )
        return card
    finally:
        observe_request(
            method="GET",
            route="/v1/models",
            status=status,
            cached=False,
            stream=False,
            seconds=time.perf_counter() - started,
        )
