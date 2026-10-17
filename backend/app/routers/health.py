from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import db

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> JSONResponse:
    db_ok = db.check_db()
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status": "ok" if db_ok else "error",
            "timestamp": datetime.now(UTC).isoformat(),
            "db": "ok" if db_ok else "error",
        },
    )
