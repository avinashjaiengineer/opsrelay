"""A specialist agent served over the A2A protocol.

On AgentCore Runtime (protocol A2A) the container must serve A2A JSON-RPC at "/" on port
9000, the agent card at /.well-known/agent-card.json, and /ping. `build_a2a_app` from the
AgentCore SDK provides exactly that, plus propagation of the runtime's session headers.
"""

import os

from a2a.types import AgentCard, AgentSkill
from bedrock_agentcore.runtime.a2a import build_a2a_app
from strands.multiagent.a2a import A2AServer, StrandsA2AExecutor

from .. import __version__
from ..a2a_auth import protect
from ..agents import build_specialist
from ..agents.prompts import DESCRIPTIONS
from ..config import SPECIALISTS, Role
from ..environment import get_environment
from ..store import get_store

A2A_PORT = 9000


def build_app(role: Role, public_url: str | None = None):
    if role not in SPECIALISTS:
        raise ValueError(f"{role} is not a specialist; choose from {SPECIALISTS}")
    store = get_store()
    env = get_environment(store)

    def factory(_context_id: str):
        return build_specialist(role, store, env)

    url = public_url or os.environ.get("AGENTCORE_RUNTIME_URL") or f"http://localhost:{A2A_PORT}/"
    # Advertise the agent's capability, not its internal tools.
    skill = AgentSkill(
        id=f"incident-{role}",
        name=f"Incident {role}",
        description=DESCRIPTIONS[role],
        tags=["it-operations", "incident-response", role],
        examples=[f"Incident inc-0123456789: {role} this incident."],
    )
    card: AgentCard = A2AServer(
        agent_factory=factory, http_url=url, serve_at_root=True, version=__version__, skills=[skill]
    ).public_agent_card
    return protect(
        build_a2a_app(
            StrandsA2AExecutor(agent_factory=factory, enable_a2a_compliant_streaming=True), card, runtime_url=url
        )
    )


def serve(role: Role, port: int = A2A_PORT, host: str = "0.0.0.0") -> None:  # noqa: S104 - container entrypoint
    import uvicorn

    uvicorn.run(build_app(role, os.environ.get("OPSRELAY_PUBLIC_URL")), host=host, port=port)
