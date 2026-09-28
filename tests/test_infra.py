"""Synthesizes the CDK stack (no AWS account or Docker needed) and checks the topology."""

import json
import sys
from pathlib import Path

import pytest

cdk = pytest.importorskip("aws_cdk")
assertions = pytest.importorskip("aws_cdk.assertions")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "infra"))
from stack import OpsRelayStack  # noqa: E402


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    app = cdk.App(outdir=str(tmp_path_factory.mktemp("cdk")))
    stack = OpsRelayStack(app, "Test", env=cdk.Environment(account="123456789012", region="us-east-1"))
    return assertions.Template.from_stack(stack)


def test_six_runtimes_with_expected_protocols(template):
    runtimes = template.find_resources("AWS::BedrockAgentCore::Runtime")
    protocols = {
        r["Properties"]["AgentRuntimeName"]: r["Properties"]["ProtocolConfiguration"] for r in runtimes.values()
    }
    assert protocols == {
        "opsrelay_coordinator": "HTTP",
        "opsrelay_triage": "A2A",
        "opsrelay_diagnostics": "A2A",
        "opsrelay_remediation": "A2A",
        "opsrelay_verification": "A2A",
        "opsrelay_communications": "A2A",
    }


def test_coordinator_is_wired_to_specialists_over_a2a(template):
    runtimes = template.find_resources("AWS::BedrockAgentCore::Runtime")
    [coordinator] = [r for r in runtimes.values() if r["Properties"]["AgentRuntimeName"] == "opsrelay_coordinator"]
    env = coordinator["Properties"]["EnvironmentVariables"]
    assert env["OPSRELAY_SPECIALIST_TRANSPORT"] == "a2a"
    assert env["OPSRELAY_STORE"] == "dynamodb"
    for role in ("TRIAGE", "DIAGNOSTICS", "REMEDIATION", "VERIFICATION", "COMMUNICATIONS"):
        assert "Fn::GetAtt" in env[f"OPSRELAY_{role}_ENDPOINT"]


def test_coordinator_may_invoke_specialists(template):
    policies = template.find_resources("AWS::IAM::Policy")
    [coordinator_policy] = [p for name, p in policies.items() if name.startswith("Coordinator")]
    actions = [s["Action"] for s in coordinator_policy["Properties"]["PolicyDocument"]["Statement"]]
    assert actions.count("bedrock-agentcore:InvokeAgentRuntime") == 5


def _statements(template, prefix):
    policies = template.find_resources("AWS::IAM::Policy")
    [policy] = [p for name, p in policies.items() if name.startswith(prefix)]
    return policy["Properties"]["PolicyDocument"]["Statement"]


def test_bedrock_access_is_scoped_to_the_configured_model(template):
    for prefix in ("Coordinator", "Triage"):
        [bedrock] = [s for s in _statements(template, prefix) if "bedrock:InvokeModel" in s["Action"]]
        resources = json.dumps(bedrock["Resource"])
        assert "nova-2-lite" in resources
        assert "foundation-model/*" not in resources and "inference-profile/*" not in resources


def _writable(template, prefix):
    [write] = [s for s in _statements(template, prefix) if "dynamodb:PutItem" in s["Action"]]
    return write["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]


def test_dynamodb_writes_mirror_the_agent_contracts(template):
    assert _writable(template, "Triage") == ["INC#*"]
    assert _writable(template, "Communications") == ["INC#*"]
    assert "APR#*" in _writable(template, "Remediation") and "REC#*" not in _writable(template, "Remediation")
    assert set(_writable(template, "Coordinator")) == {"INC#*", "APR#*", "REC#*", "SVC#*"}
    # Nobody gets unconditioned write access to the table.
    for prefix in ("Coordinator", "Triage", "Diagnostics", "Remediation", "Verification", "Communications"):
        for s in _statements(template, prefix):
            actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
            if any(
                a in ("dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem")
                for a in actions
            ):
                assert "Condition" in s, (prefix, s)


def test_state_table(template):
    template.has_resource_properties(
        "AWS::DynamoDB::GlobalTable",
        {"KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}]},
    )


def test_event_boundary(template):
    template.resource_count_is("AWS::SQS::Queue", 4)  # alert + job queues, each with a DLQ
    template.has_resource_properties(
        "AWS::Events::Rule",
        {"EventPattern": {"source": ["aws.cloudwatch"], "detail-type": ["CloudWatch Alarm State Change"]}},
    )
    template.has_resource_properties("AWS::Events::Rule", {"ScheduleExpression": "rate(5 minutes)"})
    modes = sorted(
        f["Properties"]["Environment"]["Variables"]["MODE"]
        for f in template.find_resources("AWS::Lambda::Function").values()
    )
    assert modes == ["intake", "jobs", "recover"]
    template.has_resource_properties(
        "AWS::Lambda::EventSourceMapping", {"FunctionResponseTypes": ["ReportBatchItemFailures"]}
    )
    for queue in template.find_resources("AWS::SQS::Queue").values():
        props = queue["Properties"]
        if "RedrivePolicy" in props:
            assert props["RedrivePolicy"]["maxReceiveCount"] == 5
            assert props["VisibilityTimeout"] > 900  # longer than the Lambdas' 15 minutes
    [coordinator] = [
        r
        for r in template.find_resources("AWS::BedrockAgentCore::Runtime").values()
        if r["Properties"]["AgentRuntimeName"] == "opsrelay_coordinator"
    ]
    assert "OPSRELAY_JOB_QUEUE_URL" in coordinator["Properties"]["EnvironmentVariables"]


def test_jwt_authorizer_needs_event_intake_off(tmp_path):
    def synth(**context):
        app = cdk.App(outdir=str(tmp_path / str(len(context))), context=context)
        return OpsRelayStack(app, "Jwt", env=cdk.Environment(account="123456789012", region="us-east-1"))

    url = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_x/.well-known/openid-configuration"
    with pytest.raises(ValueError, match="either IAM or JWT"):
        synth(jwt_discovery_url=url)
    stack = synth(jwt_discovery_url=url, jwt_allowed_clients="client-1", event_intake="false")
    template = assertions.Template.from_stack(stack)
    template.resource_count_is("AWS::Lambda::Function", 0)
    [coordinator] = [
        r
        for r in template.find_resources("AWS::BedrockAgentCore::Runtime").values()
        if r["Properties"]["AgentRuntimeName"] == "opsrelay_coordinator"
    ]
    jwt = coordinator["Properties"]["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert jwt["DiscoveryUrl"] == url and jwt["AllowedClients"] == ["client-1"]
    assert coordinator["Properties"]["RequestHeaderConfiguration"]["RequestHeaderAllowlist"] == ["Authorization"]
