"""FastAPI application entry point: uvicorn app.main:app"""

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.routers import health, orders, webhooks

logger = logging.getLogger("app")

settings = get_settings()

app = FastAPI(
    title="ecomsellercentral API",
    version="0.1.0",
    # Interactive docs at /docs in every environment; data routes get auth in #14.
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(orders.router)
app.include_router(webhooks.router)


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """Any uncaught error becomes JSON 500; details only outside production."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    content: dict[str, str] = {"detail": "Internal Server Error"}
    if not get_settings().is_production:
        content["error"] = f"{type(exc).__name__}: {exc}"
    return JSONResponse(status_code=500, content=content)
