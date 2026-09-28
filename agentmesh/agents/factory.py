"""Builds the Strands agents."""

import logging
from collections.abc import Awaitable, Callable

from strands import Agent, tool
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry
from strands.models.model import Model

from ..config import SPECIALISTS, Role, get_settings
from ..environment import Environment
from ..offline import POLICIES, ScriptedModel
from ..store import Store
from . import tools
from .prompts import DESCRIPTIONS, PROMPTS

log = logging.getLogger(__name__)

# Sends a message to a specialist agent and returns its final text reply.
Invoker = Callable[[str], Awaitable[str]]


def build_model(role: Role) -> Model:
    settings = get_settings()
    if settings.model_provider == "offline":
        return ScriptedModel(POLICIES[role], name=role)
    from strands.models import BedrockModel

    return BedrockModel(
        model_id=settings.bedrock_model_id,
        region_name=settings.aws_region,
        max_tokens=settings.max_tokens,
    )


class AuditHook(HookProvider):
    """Writes every tool call an agent makes to the incident's audit log."""

    def __init__(self, store: Store, actor: str):
        self.store = store
        self.actor = actor

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:  # noqa: ANN003
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        name = event.tool_use["name"]
        if name.startswith("ask_"):
            return  # delegations are logged by the delegation tool itself
        tool_input = event.tool_use.get("input") or {}
        result = event.result or {}
        text = "".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
        try:
            self.store.record(
                tool_input.get("incident_id") if isinstance(tool_input, dict) else None,
                self.actor,
                "tool.call",
                name,
                {"input": tool_input, "status": result.get("status"), "result": text[:500]},
            )
        except Exception:  # noqa: BLE001 - auditing must never break the agent
            log.exception("failed to record tool call")


def _agent(role: Role, store: Store, agent_tools: list) -> Agent:
    return Agent(
        name=f"agentmesh-{role}",
        description=DESCRIPTIONS[role],
        model=build_model(role),
        system_prompt=PROMPTS[role],
        tools=agent_tools,
        hooks=[AuditHook(store, role)],
        callback_handler=None,
    )


def build_specialist(role: Role, store: Store, env: Environment) -> Agent:
    if role == "triage":
        agent_tools = tools.triage_tools(store, env, role)
    elif role == "diagnostics":
        agent_tools = tools.diagnostics_tools(store, env, role)
    elif role == "remediation":
        agent_tools = tools.remediation_tools(store, env, role)
    elif role == "communications":
        agent_tools = tools.communications_tools(store, role)
    else:
        raise ValueError(f"{role} is not a specialist")
    return _agent(role, store, agent_tools)


def _delegation_tool(role: Role, invoke: Invoker, store: Store):
    @tool(name=f"ask_{role}", description=f"Delegate to the {role} agent over A2A. {DESCRIPTIONS[role]}")
    async def ask(incident_id: str, request: str) -> str:
        """Send a request to a specialist agent and return its report.

        Args:
            incident_id: The incident id the request is about.
            request: What you need the specialist to do.
        """
        message = f"Incident {incident_id}: {request}"
        store.record(incident_id, "coordinator", "a2a.request", f"-> {role}: {request}")
        try:
            reply = await invoke(message)
        except Exception as e:  # noqa: BLE001 - a failed delegation is a result the coordinator can act on
            log.exception("delegation to %s failed", role)
            store.record(incident_id, "coordinator", "a2a.error", f"{role} failed: {e}")
            return f"ERROR: the {role} agent failed: {type(e).__name__}: {e}"
        store.record(incident_id, role, "a2a.response", reply[:2000])
        return reply

    return ask


def local_invoker(role: Role, store: Store, env: Environment) -> Invoker:
    """Runs the specialist in this process, a fresh agent per request."""

    async def invoke(message: str) -> str:
        result = await build_specialist(role, store, env).invoke_async(message)
        return str(result).strip()

    return invoke


def build_coordinator(store: Store, env: Environment, invokers: dict[Role, Invoker] | None = None) -> Agent:
    """The coordinator agent. `invokers` maps each specialist to how it is reached
    (in-process or over A2A); by default every specialist runs in-process."""
    invokers = invokers or {role: local_invoker(role, store, env) for role in SPECIALISTS}
    delegation = [_delegation_tool(role, invokers[role], store) for role in SPECIALISTS]
    return _agent("coordinator", store, [*tools.coordinator_tools(store, "coordinator"), *delegation])
