import pytest

from opsrelay.config import get_settings
from opsrelay.environment import SimulatedEnvironment
from opsrelay.policy import get_policy
from opsrelay.resilience import reset_breakers
from opsrelay.service import IncidentService
from opsrelay.store import get_store
from opsrelay.store.sqlite import SqliteStore


def _clear_caches() -> None:
    get_settings.cache_clear()
    get_store.cache_clear()
    get_policy.cache_clear()
    reset_breakers()


@pytest.fixture(autouse=True)
def _settings(monkeypatch, tmp_path):
    """Offline agents, in-process specialists, a fresh SQLite file per test, no retry delays."""
    for key in ("MODEL_PROVIDER", "SPECIALIST_TRANSPORT", "STORE", "POLICY_FILE"):
        monkeypatch.delenv(f"OPSRELAY_{key}", raising=False)
    monkeypatch.setenv("OPSRELAY_MODEL_PROVIDER", "offline")
    monkeypatch.setenv("OPSRELAY_SQLITE_PATH", str(tmp_path / "opsrelay.db"))
    monkeypatch.setenv("OPSRELAY_RETRY_BACKOFF_SECONDS", "0")
    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture
def store(tmp_path):
    return SqliteStore(str(tmp_path / "test.db"))


@pytest.fixture
def env(store):
    environment = SimulatedEnvironment(store)
    environment.seed()
    return environment


@pytest.fixture
def service(store, env):
    return IncidentService(store=store, env=env)
