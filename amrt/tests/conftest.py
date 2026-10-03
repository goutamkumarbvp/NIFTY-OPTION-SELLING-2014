import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from amrt.security.identity import set_denial_audit_hook  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_hook():
    yield
    set_denial_audit_hook(None)


def pg_url() -> str | None:
    url = os.environ.get("AMRT_TEST_PG_URL")
    if not url:
        return None
    try:
        import sqlalchemy as sa
        eng = sa.create_engine(url)
        with eng.connect() as c:
            c.execute(sa.text("SELECT 1"))
        eng.dispose()
        return url
    except Exception:
        return None


@pytest.fixture
def pg_db_url():
    """A fresh PostgreSQL database per test (skipped when no server is configured)."""
    base = pg_url()
    if not base:
        pytest.skip("PostgreSQL not available (set AMRT_TEST_PG_URL; see deploy/scripts/local_pg.sh)")
    import uuid

    import sqlalchemy as sa
    name = "amrt_t_" + uuid.uuid4().hex[:10]
    admin = sa.create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(sa.text(f"CREATE DATABASE {name}"))
    url = base.rsplit("/", 1)[0] + "/" + name
    yield url
    with admin.connect() as c:
        c.execute(sa.text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{name}' AND pid <> pg_backend_pid()"))
        c.execute(sa.text(f"DROP DATABASE IF EXISTS {name}"))
    admin.dispose()


@pytest.fixture
def redis_url():
    url = os.environ.get("AMRT_TEST_REDIS_URL")
    if not url:
        pytest.skip("Redis not available (set AMRT_TEST_REDIS_URL)")
    try:
        import redis
        redis.Redis.from_url(url).ping()
    except Exception:
        pytest.skip("Redis not reachable")
    return url
