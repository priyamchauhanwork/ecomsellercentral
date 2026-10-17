import os

# Tests never need a real .env; fall back to the local Docker database from SETUP.md.
os.environ.setdefault("DATABASE_URL", "postgresql://mvp:mvp_local@localhost:5432/mvp_db")
