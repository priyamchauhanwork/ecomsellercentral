"""Shopify order and return webhooks (#11, #53): verify the signature, record the
delivery, answer at once, and process it in the background (docs/order-erd.md,
"Webhook"; docs/inventory-erd.md)."""

import base64
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.db import SessionLocal
from app.services.order_ingestion import (
    RETURN_TOPICS,
    record_webhook_delivery,
    run_webhook_import,
)
from app.services.shopify import normalize_store_domain

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Topics whose payload is a full order. Others are acknowledged and ignored, so that
# Shopify doesn't keep retrying them.
ORDER_TOPICS = {
    "orders/create",
    "orders/updated",
    "orders/cancelled",
    "orders/fulfilled",
    "orders/partially_fulfilled",
    "orders/paid",
}


def signature_is_valid(body: bytes, received: str, secret: str) -> bool:
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest())
    return hmac.compare_digest(expected, received.encode())


def record(topic: str, delivery_id: str | None, payload: object) -> tuple[int, int] | None:
    with SessionLocal.begin() as session:
        return record_webhook_delivery(session, topic, delivery_id, payload)


@router.post("/orders")
async def shopify_order_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive an order webhook from Shopify. Must answer within 5 seconds."""
    settings = get_settings()
    secrets = settings.shopify_webhook_secrets
    if not secrets:  # 503 so Shopify retries once the secret is configured
        logger.error("Shopify webhook received but no signing secret is configured")
        raise HTTPException(503, "Webhook signing secret is not configured")

    body = await request.body()  # the exact bytes Shopify signed
    headers = request.headers
    received = headers.get("X-Shopify-Hmac-Sha256", "")
    if not any(signature_is_valid(body, received, secret) for secret in secrets):
        logger.warning(
            "Rejected Shopify webhook with an invalid signature (topic=%s, shop=%s)",
            headers.get("X-Shopify-Topic"),
            headers.get("X-Shopify-Shop-Domain"),
        )
        raise HTTPException(401, "Invalid webhook signature")

    topic = headers.get("X-Shopify-Topic", "")
    if topic not in ORDER_TOPICS | RETURN_TOPICS:
        logger.info("Ignored Shopify webhook topic %r", topic)
        return {"status": "ignored", "reason": f"unsupported topic {topic!r}"}

    shop = normalize_store_domain(headers.get("X-Shopify-Shop-Domain", ""))
    if shop and shop != normalize_store_domain(settings.shopify_store_domain):
        logger.warning("Ignored Shopify webhook from another store: %s", shop)
        return {"status": "ignored", "reason": f"unknown store {shop}"}

    try:
        payload = json.loads(body)
    except ValueError as exc:
        logger.error("Shopify webhook %s has a body that is not JSON", topic)
        raise HTTPException(400, "Body is not valid JSON") from exc

    delivery_id = headers.get("X-Shopify-Webhook-Id")
    recorded = await run_in_threadpool(record, topic, delivery_id, payload)
    if recorded is None:
        logger.info("Ignored repeat delivery of Shopify webhook %s", delivery_id)
        return {"status": "duplicate"}

    order_import_id, row_id = recorded
    background_tasks.add_task(run_webhook_import, order_import_id, row_id)
    return {"status": "accepted", "order_import_id": order_import_id}
