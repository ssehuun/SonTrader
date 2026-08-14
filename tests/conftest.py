import pytest
import sqlalchemy as sa

from sontrader.config import Settings

TOKEN_RESPONSE = {
    "access_token": "test-token",
    "token_type": "Bearer",
    "expires_in": 86400,
    "access_token_token_expired": "2099-01-01 00:00:00",
}


@pytest.fixture
def settings(tmp_path):
    return Settings(
        app_key="key",
        app_secret="secret",
        cano="12345678",
        acnt_prdt_cd="01",
        paper=True,
        token_cache=tmp_path / "token.json",
        approval_key_cache=tmp_path / "approval_key.json",
    )


@pytest.fixture
def db_engine():
    """SQLite in-memory engine with FK enforcement on, mirroring PostgreSQL."""
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    sa.event.listen(
        engine, "connect", lambda dbapi_conn, _: dbapi_conn.execute("PRAGMA foreign_keys=ON")
    )
    return engine
