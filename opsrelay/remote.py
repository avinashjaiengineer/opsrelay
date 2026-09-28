"""Calling specialist agents over the A2A protocol.

An endpoint is either a plain http(s) A2A URL (local servers, other platforms) or an
AgentCore Runtime ARN. For an ARN the request goes to the AgentCore invocation URL, signed
with SigV4 using the caller's IAM role, with a runtime session id header.
"""

import uuid
from collections.abc import Generator

import boto3
import httpx
from a2a.client import ClientConfig
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from strands.agent.a2a_agent import A2AAgent
from strands.agent.agent_result import AgentResult

from .agents.factory import Invoker
from .config import Role

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


class AgentCoreSigV4(httpx.Auth):
    """httpx auth that SigV4-signs requests for the bedrock-agentcore service."""

    requires_request_body = True

    def __init__(self, region: str, session: boto3.Session | None = None):
        self.region = region
        self.session = session or boto3.Session()

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        credentials = self.session.get_credentials()
        if credentials is None:
            raise RuntimeError("No AWS credentials available to sign the A2A request")
        signed_headers = {
            k: v for k, v in request.headers.items() if k.lower() in ("host", "content-type", SESSION_HEADER.lower())
        }
        aws_request = AWSRequest(
            method=request.method, url=str(request.url), data=request.content, headers=signed_headers
        )
        SigV4Auth(credentials.get_frozen_credentials(), "bedrock-agentcore", self.region).add_auth(aws_request)
        request.headers.update(dict(aws_request.headers))
        yield request


def resolve_endpoint(endpoint: str) -> tuple[str, httpx.Auth | None, dict[str, str]]:
    """Return (base_url, auth, headers) for an A2A endpoint or an AgentCore runtime ARN."""
    if endpoint.startswith("arn:"):
        from bedrock_agentcore.runtime.a2a import build_runtime_url

        region = endpoint.split(":")[3]
        # AgentCore requires a session id of at least 33 characters.
        headers = {SESSION_HEADER: f"opsrelay-{uuid.uuid4().hex}"}
        return build_runtime_url(endpoint), AgentCoreSigV4(region), headers
    return endpoint.rstrip("/"), None, {}


def reply_text(result: AgentResult) -> str:
    """The specialist's reply as one string.

    A streamed A2A reply arrives as one text block per chunk. `str(AgentResult)` puts a newline
    after every block, which splits the reply mid-word, so join the blocks as they are.
    """
    blocks = result.message.get("content", [])
    return "".join(b["text"] for b in blocks if isinstance(b, dict) and "text" in b).strip()


def a2a_invoker(role: Role, endpoint: str, timeout: int = 600) -> Invoker:
    if not endpoint:
        raise ValueError(f"No A2A endpoint configured for the {role} agent (OPSRELAY_{role.upper()}_ENDPOINT)")

    async def invoke(message: str) -> str:
        url, auth, headers = resolve_endpoint(endpoint)
        async with httpx.AsyncClient(timeout=timeout, auth=auth, headers=headers) as client:
            agent = A2AAgent(url, client_config=ClientConfig(httpx_client=client), timeout=timeout)
            card = await agent.get_agent_card()
            # A runtime cannot know its own ARN when it is built, so its card may advertise a
            # placeholder URL. Always send to the endpoint we resolved.
            card.url = url + "/"
            return reply_text(await agent.invoke_async(message))

    return invoke
