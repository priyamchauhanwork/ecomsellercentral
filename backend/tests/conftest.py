"""Tests run against their own database (mvp_test), never the development mvp_db.

TEST_DATABASE_URL overrides the default. The database is created if missing and
migrated to head before the tests; DB tests are skipped if Postgres is unreachable.
"""

import os

import psycopg
import pytest
from sqlalchemy.engine import make_url

# Shared fixtures (shopify, watches) for the supply & demand tests.
pytest_plugins = ["tests.stock_helpers"]

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://mvp:mvp_local@localhost:5432/mvp_test"
)
# Must happen before app modules are imported: settings and the engine read it once.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# Tests call the API without a key unless they set one (a developer's .env may hold a key).
os.environ["API_SHARED_SECRET"] = ""


def _create_test_database() -> bool:
    url = make_url(TEST_DATABASE_URL)
    try:
        with psycopg.connect(
            host=url.host,
            port=url.port or 5432,
            user=url.username,
            password=url.password,
            dbname="postgres",
            autocommit=True,
            connect_timeout=3,
        ) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (url.database,)
            ).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{url.database}"')
        return True
    except psycopg.OperationalError:
        return False


DB_AVAILABLE = _create_test_database()


@pytest.fixture(scope="session", autouse=True)
def migrated_test_database():
    if DB_AVAILABLE:
        from alembic import command
        from alembic.config import Config

        from app.config import BACKEND_DIR

        command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    yield


# Sellers and channels are kept: the migration creates seller 1, which orders need.
ECS_TABLES = (
    "ecs_inventory_log, ecs_inventory_supply, ecs_inventory_demand, ecs_return_lines, "
    "ecs_returns, ecs_order_line_history, ecs_order_history, ecs_order_line, "
    "ecs_order_import_row, "
    "ecs_order_header, ecs_order_import, ecs_channel_listing_items, ecs_channel_listings, "
    "ecs_items, ecs_articles, ecs_channel_accounts"
)


@pytest.fixture
def clean_db():
    """Empty every order table before (and after) a test that commits data."""
    if not DB_AVAILABLE:
        pytest.skip("needs the local Postgres from SETUP.md")
    from sqlalchemy import text

    from app.db import engine

    def truncate():
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {ECS_TABLES} RESTART IDENTITY CASCADE"))

    truncate()
    yield
    truncate()
