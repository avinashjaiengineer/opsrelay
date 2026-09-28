"""Builds the Strands agents, and enforces their contracts at every delegation."""

import json
import logging
import time
from collections.abc import Awaitable, Callable

from strands import Agent, tool
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry
from strands.models.model import Model

from .. import telemetry
from ..config import SPECIALISTS, Role, get_settings
from ..contracts import CONTRACTS, COORDINATOR_TOOLS
from ..deadletter import dead_letter
from ..environment import Environment
from ..lifecycle import IllegalTransition, Status, status_of, transition
from ..offline import POLICIES, ScriptedModel
from ..resilience import Attempt, call_with_retry
from ..store import Record, Store
from . import tools
from .meta import agent_meta
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
    """Writes every tool call an agent makes to the incident's audit log, before and after."""

    def __init__(self, store: Store, actor: str):
        self.store = store
        self.actor = actor

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:  # noqa: ANN003
        registry.add_callback(BeforeToolCallEvent, self._before_tool)
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _record(self, kind: str, tool_use: dict, data: dict, **hashes) -> None:  # noqa: ANN003
        tool_input = tool_use.get("input") or {}
        incident_id = tool_input.get("incident_id") if isinstance(tool_input, dict) else None
        try:
            self.store.record(
                incident_id, self.actor, kind, tool_use["name"], {**data, **agent_meta(self.actor)}, **hashes
            )
        except Exception:  # noqa: BLE001 - auditing must never break the agent
            log.exception("failed to record %s", kind)

    def _before_tool(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"].startswith("ask_"):
            return  # delegations are logged by the delegation tool itself
        tool_input = event.tool_use.get("input") or {}
        self._record("tool.invoked", event.tool_use, {"input": tool_input}, input=tool_input)

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        if event.tool_use["name"].startswith("ask_"):
            return
        result = event.result or {}
        text = "".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
        self._record(
            "tool.completed",
            event.tool_use,
            {"input": event.tool_use.get("input") or {}, "status": result.get("status"), "result": text[:500]},
            output=text,
        )


def _agent(role: Role, store: Store, agent_tools: list) -> Agent:
    return Agent(
        name=f"opsrelay-{role}",
        description=DESCRIPTIONS[role],
        model=build_model(role),
        system_prompt=PROMPTS[role],
        tools=agent_tools,
        hooks=[AuditHook(store, role)],
        callback_handler=None,
    )


def _check_tools(role: str, agent_tools: list, allowed: frozenset[str]) -> list:
    names = {t.tool_name for t in agent_tools}
    if names != allowed:
        raise RuntimeError(f"{role} tools {sorted(names)} do not match its contract {sorted(allowed)}")
    return agent_tools


def build_specialist(role: Role, store: Store, env: Environment) -> Agent:
    if role not in CONTRACTS:
        raise ValueError(f"{role} is not a specialist")
    if role == "communications":
        agent_tools = tools.communications_tools(store, role)
    else:
        agent_tools = tools.TOOL_FACTORIES[role](store, env, role)
    return _agent(role, store, _check_tools(role, agent_tools, CONTRACTS[role].tools))


def _latest_result(role: str, events: list[Record]) -> dict | None:
    for event in reversed(events):
        result = (event.get("data") or {}).get("result")
        if event["actor"] == role and not event["kind"].startswith("tool.") and isinstance(result, dict):
            return {"kind": event["kind"], **result}
    return None


def _delegation_tool(role: Role, invoke: Invoker, store: Store):
    contract = CONTRACTS[role]
    states = ", ".join(sorted(contract.acts_in))

    @tool(
        name=f"ask_{role}",
        description=f"Delegate to the {role} agent over A2A. {contract.purpose} "
        f"It acts only on incidents that are: {states}.",
    )
    async def ask(incident_id: str, request: str) -> str:
        """Send a request to a specialist agent. Returns its typed result and the incident's new status.

        Args:
            incident_id: The incident id the request is about.
            request: What you need the specialist to do.
        """
        incident = store.get_incident(incident_id)
        if incident is None:
            return json.dumps({"error": f"Unknown incident {incident_id}"})
        status = status_of(incident)
        if role == "triage" and status is Status.OPEN:
            try:
                incident = transition(store, incident_id, Status.TRIAGING, actor="coordinator", reason="sent to triage")
                status = Status.TRIAGING
            except IllegalTransition as e:
                return json.dumps({"error": str(e)})
        problem = contract.check_dispatch(status)
        if problem:
            store.record(
                incident_id,
                "platform",
                "contract.violation",
                f"Refused to dispatch {role}: {problem}",
                {"agent": role, "phase": "dispatch"},
            )
            return json.dumps({"error": problem, "incident_status": str(status)})

        before = incident
        seen = len(store.list_events(incident_id))
        store.record(incident_id, "coordinator", "a2a.request", f"-> {role}: {request}", {"agent": role})
        message = f"Incident {incident_id}: {request}"
        attempts: list[str] = []

        def new_events() -> list[Record]:
            return store.list_events(incident_id)[seen:]

        def done() -> bool:
            after = store.get_incident(incident_id)
            return after is not None and contract.postcondition(before, after, new_events()) is None

        def on_retry(attempt: Attempt) -> None:
            attempts.append(attempt.error)
            store.record(
                incident_id,
                "platform",
                "agent.retry",
                f"{role} attempt {attempt.number} failed: {attempt.error}",
                {"agent": role, "attempt": attempt.number},
            )

        started = time.monotonic()
        try:
            with telemetry.span("opsrelay.delegate", incident_id=incident_id, agent=role):
                reply = await call_with_retry(role, lambda: invoke(message), done=done, on_retry=on_retry)
        except Exception as e:  # noqa: BLE001 - an unavailable agent is a result the platform handles
            telemetry.count("agent_calls_total", agent=role, outcome="unavailable")
            log.warning("delegation to %s failed: %s", role, e)
            letter = dead_letter(store, incident_id, role, request, attempts or [f"{type(e).__name__}: {e}"], str(e))
            after = store.get_incident(incident_id) or before
            return json.dumps(
                {
                    "error": f"The {role} agent is unavailable ({e}). The request went to the dead-letter queue "
                    f"({letter['id']}) and the incident was handed to a human.",
                    "incident_status": after["status"],
                }
            )

        telemetry.observe("agent_latency_seconds", time.monotonic() - started, agent=role)
        after = store.get_incident(incident_id) or before
        events = new_events()
        store.record(incident_id, role, "a2a.response", reply[:2000], {**agent_meta(role)})
        problem = contract.postcondition(before, after, events)
        telemetry.count("agent_calls_total", agent=role, outcome="contract_violation" if problem else "ok")
        if problem:
            store.record(
                incident_id, "platform", "contract.violation", f"{role}: {problem}", {"agent": role, "phase": "result"}
            )
            return json.dumps(
                {
                    "agent": role,
                    "contract_violation": problem,
                    "incident_status": after["status"],
                    "reply": reply[:1000],
                }
            )
        return json.dumps(
            {
                "agent": role,
                "result": _latest_result(role, events),
                "incident_status": after["status"],
                "summary": reply[:1000],
            },
            default=str,
        )

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
    agent_tools = [*tools.coordinator_tools(store, "coordinator"), *delegation]
    return _agent("coordinator", store, _check_tools("coordinator", agent_tools, COORDINATOR_TOOLS))
