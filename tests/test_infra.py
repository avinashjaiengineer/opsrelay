"""Synthesizes the CDK stack (no AWS account or Docker needed) and checks the topology."""

import sys
from pathlib import Path

import pytest

cdk = pytest.importorskip("aws_cdk")
assertions = pytest.importorskip("aws_cdk.assertions")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "infra"))
from stack import AgentMeshStack  # noqa: E402


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    app = cdk.App(outdir=str(tmp_path_factory.mktemp("cdk")))
    stack = AgentMeshStack(app, "Test", env=cdk.Environment(account="123456789012", region="us-east-1"))
    return assertions.Template.from_stack(stack)


def test_five_runtimes_with_expected_protocols(template):
    runtimes = template.find_resources("AWS::BedrockAgentCore::Runtime")
    protocols = {
        r["Properties"]["AgentRuntimeName"]: r["Properties"]["ProtocolConfiguration"] for r in runtimes.values()
    }
    assert protocols == {
        "agentmesh_coordinator": "HTTP",
        "agentmesh_triage": "A2A",
        "agentmesh_diagnostics": "A2A",
        "agentmesh_remediation": "A2A",
        "agentmesh_communications": "A2A",
    }


def test_coordinator_is_wired_to_specialists_over_a2a(template):
    runtimes = template.find_resources("AWS::BedrockAgentCore::Runtime")
    [coordinator] = [r for r in runtimes.values() if r["Properties"]["AgentRuntimeName"] == "agentmesh_coordinator"]
    env = coordinator["Properties"]["EnvironmentVariables"]
    assert env["AGENTMESH_SPECIALIST_TRANSPORT"] == "a2a"
    assert env["AGENTMESH_STORE"] == "dynamodb"
    for role in ("TRIAGE", "DIAGNOSTICS", "REMEDIATION", "COMMUNICATIONS"):
        assert "Fn::GetAtt" in env[f"AGENTMESH_{role}_ENDPOINT"]


def test_coordinator_may_invoke_specialists(template):
    policies = template.find_resources("AWS::IAM::Policy")
    [coordinator_policy] = [p for name, p in policies.items() if name.startswith("Coordinator")]
    actions = [s["Action"] for s in coordinator_policy["Properties"]["PolicyDocument"]["Statement"]]
    assert actions.count("bedrock-agentcore:InvokeAgentRuntime") == 4


def test_state_table(template):
    template.has_resource_properties(
        "AWS::DynamoDB::GlobalTable",
        {"KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}]},
    )
