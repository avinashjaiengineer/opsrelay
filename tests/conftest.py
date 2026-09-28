import pytest

from agentmesh.config import get_settings
from agentmesh.environment import SimulatedEnvironment
from agentmesh.service import IncidentService
from agentmesh.store import get_store
from agentmesh.store.sqlite import SqliteStore


@pytest.fixture(autouse=True)
def _settings(monkeypatch, tmp_path):
    """Offline agents, in-process specialists, a fresh SQLite file per test."""
    for key in ("MODEL_PROVIDER", "SPECIALIST_TRANSPORT", "STORE", "AUTO_APPROVE_RISK"):
        monkeypatch.delenv(f"AGENTMESH_{key}", raising=False)
    monkeypatch.setenv("AGENTMESH_MODEL_PROVIDER", "offline")
    monkeypatch.setenv("AGENTMESH_SQLITE_PATH", str(tmp_path / "agentmesh.db"))
    get_settings.cache_clear()
    get_store.cache_clear()
    yield
    get_settings.cache_clear()
    get_store.cache_clear()


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
