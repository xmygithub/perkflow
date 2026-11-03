import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from backend.app.core.config import Settings
from backend.app.core.database import get_session
from backend.app.main import create_app


def test_password_is_encoded_and_hidden():
    settings = Settings(_env_file=None, mysql_password="a@b:/#?% secret")
    assert settings.database_url.password == "a@b:/#?% secret"
    assert "secret" not in repr(settings)
    assert "secret" not in str(settings.database_url)


def test_env_file_and_environment_precedence(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('MYSQL_PORT=3399\nMYSQL_PASSWORD="a@b#c"\n', encoding="utf-8")
    monkeypatch.setenv("MYSQL_PORT", "3400")
    settings = Settings(_env_file=env_file)
    assert settings.mysql_port == 3400
    assert settings.mysql_password.get_secret_value() == "a@b#c"


def test_cache_jitter_configuration_and_environment_precedence(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SHOP_CACHE_TTL_JITTER=17\nSHOP_TYPE_CACHE_TTL_JITTER=29\n", encoding="utf-8"
    )
    settings = Settings(_env_file=env_file)
    assert settings.shop_cache_ttl_jitter == 17 and settings.shop_type_cache_ttl_jitter == 29
    monkeypatch.setenv("SHOP_CACHE_TTL_JITTER", "0")
    assert Settings(_env_file=env_file).shop_cache_ttl_jitter == 0


@pytest.mark.parametrize("field", ["shop_cache_ttl_jitter", "shop_type_cache_ttl_jitter"])
@pytest.mark.parametrize("value", [-1, 3601])
def test_cache_jitter_rejects_unsafe_configuration(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    "values",
    [
        {"shop_cache_lock_ttl_ms": 0},
        {"shop_cache_lock_wait_ms": 0},
        {"shop_cache_lock_retry_ms": 0},
        {"shop_cache_lock_wait_ms": 10001},
        {"cache_lock_key_prefix": "perkflow:cache:"},
    ],
)
def test_rebuild_lock_rejects_invalid_budgets_or_colliding_namespace(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_namespace_validation_does_not_print_other_configuration_secrets():
    with pytest.raises(ValidationError) as error:
        Settings(
            _env_file=None,
            mysql_password="private-config-test-value",
            cache_lock_key_prefix="perkflow:cache:",
        )
    assert "private-config-test-value" not in str(error.value)


async def test_live_openapi_and_validation_without_database():
    app = create_app(Settings(_env_file=None))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/health/live")).json() == {"status": "ok"}
        assert (await client.get("/docs")).status_code == 200
        paths = (await client.get("/openapi.json")).json()["paths"]
        assert "/api/shops/{shop_id}" in paths
        for query in ["page=0", "page_size=51", "sort=DROP", "category_id=-1", "unknown=1"]:
            response = await client.get(f"/api/shops?{query}")
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "VALIDATION_ERROR"
            assert all("input" not in entry for entry in response.json()["error"]["details"])
        assert (await client.get("/api/shops/0")).status_code == 422
        assert (await client.get("/missing")).json()["error"]["code"] == "HTTP_404"


async def test_database_failure_is_503_without_exposing_credentials():
    app = create_app(Settings(_env_file=None, mysql_password="private-password"))

    async def unavailable():
        raise OperationalError(
            "SELECT secret", {"password": "private-password"}, RuntimeError("fail")
        )
        yield  # Dependency retains the same generator shape as get_session.

    app.dependency_overrides[get_session] = unavailable
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
        assert "private-password" not in response.text
        assert "SELECT" not in response.text


async def test_cors_only_allows_configured_frontend():
    app = create_app(Settings(_env_file=None))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        allowed = await client.get("/health/live", headers={"Origin": "http://127.0.0.1:5173"})
        assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
        denied = await client.get(
            "/health/live", headers={"Origin": "https://unconfigured.example"}
        )
        assert "access-control-allow-origin" not in denied.headers
