import boto3
import httpx
import pytest
from botocore.credentials import Credentials

from agentmesh.remote import SESSION_HEADER, AgentCoreSigV4, a2a_invoker, resolve_endpoint

ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/agentmesh_triage-AbC123"


def test_arn_resolves_to_signed_agentcore_invocation_url():
    url, auth, headers = resolve_endpoint(ARN)
    assert url.startswith("https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore")
    assert url.endswith("/invocations")
    assert isinstance(auth, AgentCoreSigV4) and auth.region == "us-west-2"
    assert len(headers[SESSION_HEADER]) >= 33


def test_plain_url_is_unsigned():
    assert resolve_endpoint("http://triage:9000/") == ("http://triage:9000", None, {})


def test_sigv4_signs_request():
    class FakeSession(boto3.Session):
        def get_credentials(self):
            return Credentials("AKIDEXAMPLE", "secret", "token")

    auth = AgentCoreSigV4("us-west-2", session=FakeSession())
    request = httpx.Request(
        "POST",
        "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/x/invocations/",
        json={"jsonrpc": "2.0"},
        headers={SESSION_HEADER: "s" * 40},
    )
    signed = next(auth.auth_flow(request))
    authz = signed.headers["authorization"]
    assert authz.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-west-2/bedrock-agentcore/aws4_request" in authz
    assert "x-amzn-bedrock-agentcore-runtime-session-id" in authz
    assert signed.headers["x-amz-security-token"] == "token"


def test_missing_endpoint_is_a_clear_error():
    with pytest.raises(ValueError, match="AGENTMESH_TRIAGE_ENDPOINT"):
        a2a_invoker("triage", "")
