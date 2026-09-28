from strands.models import BedrockModel

from opsrelay.agents import build_coordinator, build_specialist
from opsrelay.agents.factory import build_model
from opsrelay.config import SPECIALISTS, get_settings
from opsrelay.contracts import CONTRACTS, COORDINATOR_TOOLS
from opsrelay.offline import ScriptedModel


def test_bedrock_model_is_configured_from_settings(monkeypatch):
    monkeypatch.setenv("OPSRELAY_MODEL_PROVIDER", "bedrock")
    monkeypatch.setenv("OPSRELAY_BEDROCK_MODEL_ID", "us.anthropic.claude-opus-5")
    monkeypatch.setenv("OPSRELAY_AWS_REGION", "eu-west-1")
    get_settings.cache_clear()

    model = build_model("triage")

    assert isinstance(model, BedrockModel)
    assert model.get_config()["model_id"] == "us.anthropic.claude-opus-5"
    assert model.client.meta.region_name == "eu-west-1"


def test_offline_is_the_default():
    assert isinstance(build_model("coordinator"), ScriptedModel)


def test_each_agent_gets_exactly_its_contracts_tools(store, env):
    tools = {role: set(build_specialist(role, store, env).tool_names) for role in SPECIALISTS}
    tools["coordinator"] = set(build_coordinator(store, env).tool_names)

    for role in SPECIALISTS:
        assert tools[role] == CONTRACTS[role].tools, role
    assert tools["coordinator"] == COORDINATOR_TOOLS
    # Least privilege: only remediation can propose, nobody can execute, only communications resolves.
    assert all("submit_proposal" not in t for role, t in tools.items() if role != "remediation")
    assert not any("execute" in name for t in tools.values() for name in t)
    assert all("submit_postmortem" not in t for role, t in tools.items() if role != "communications")
    assert "get_metrics" not in tools["communications"]
    assert not any(name.startswith("ask_") for role in SPECIALISTS for name in tools[role])
