from strands.models import BedrockModel

from agentmesh.agents import build_coordinator, build_specialist
from agentmesh.agents.factory import build_model
from agentmesh.config import SPECIALISTS, get_settings
from agentmesh.offline import ScriptedModel


def test_bedrock_model_is_configured_from_settings(monkeypatch):
    monkeypatch.setenv("AGENTMESH_MODEL_PROVIDER", "bedrock")
    monkeypatch.setenv("AGENTMESH_BEDROCK_MODEL_ID", "us.anthropic.claude-opus-5")
    monkeypatch.setenv("AGENTMESH_AWS_REGION", "eu-west-1")
    get_settings.cache_clear()

    model = build_model("triage")

    assert isinstance(model, BedrockModel)
    assert model.get_config()["model_id"] == "us.anthropic.claude-opus-5"
    assert model.client.meta.region_name == "eu-west-1"


def test_offline_is_the_default():
    assert isinstance(build_model("coordinator"), ScriptedModel)


def test_each_agent_gets_only_its_own_tools(store, env):
    tools = {role: set(build_specialist(role, store, env).tool_names) for role in SPECIALISTS}
    tools["coordinator"] = set(build_coordinator(store, env).tool_names)

    assert "propose_action" in tools["remediation"]
    # Only remediation can propose actions, and nobody can execute them directly.
    assert all("propose_action" not in t for role, t in tools.items() if role != "remediation")
    assert not any("execute" in name for t in tools.values() for name in t)
    assert "resolve_incident" in tools["communications"]
    assert {f"ask_{role}" for role in SPECIALISTS} <= tools["coordinator"]
    assert not any(name.startswith("ask_") for role in SPECIALISTS for name in tools[role])
