"""Operational metrics computed from the audit trail: no metrics backend needed.

    time_to_detect      alert started (at the source)       -> incident opened
    time_to_diagnose    incident opened                     -> diagnosis submitted
    time_to_acknowledge approval requested                  -> a person decided (MTTA)
    time_to_remediate   remediating                         -> the action finished (verifying/failed)
    time_to_verify      verifying                           -> verification submitted
    time_to_resolve     incident opened                     -> resolved (MTTR)

plus incident outcomes, per-agent call counts, latency and failures, policy decisions and alert
intake. Durations are reported as count, median and 90th percentile, in seconds. The same signals
are exported live as OpenTelemetry metrics (opsrelay.telemetry) when a collector is configured.
"""

from collections import Counter, defaultdict
from datetime import datetime

from .lifecycle import TERMINAL
from .store import Record, Store

STAGES = (
    "time_to_detect",
    "time_to_diagnose",
    "time_to_acknowledge",
    "time_to_remediate",
    "time_to_verify",
    "time_to_resolve",
)


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _seconds(start: str | None, end: str | None) -> float | None:
    a, b = _ts(start), _ts(end)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())


def summarize(values: list[float]) -> Record:
    if not values:
        return {"count": 0, "median": None, "p90": None}
    ordered = sorted(values)

    def rank(q: float) -> float:
        return round(ordered[min(len(ordered) - 1, max(0, int(q * len(ordered) + 0.999999) - 1))], 1)

    return {"count": len(ordered), "median": rank(0.5), "p90": rank(0.9)}


def _first(events: list[Record], predicate) -> Record | None:  # noqa: ANN001
    return next((e for e in events if predicate(e)), None)


def _entered(events: list[Record], status: str) -> Record | None:
    return _first(events, lambda e: e["kind"] == "status.changed" and (e.get("data") or {}).get("to") == status)


def compute(store: Store, limit: int = 200) -> Record:
    incidents = store.list_incidents(limit=limit)
    stages: dict[str, list[float]] = defaultdict(list)
    statuses: Counter = Counter()
    sources: Counter = Counter()
    decisions: Counter = Counter()
    alerts: Counter = Counter()
    agent_latency: dict[str, list[float]] = defaultdict(list)
    agent_calls: Counter = Counter()
    agent_failures: Counter = Counter()

    for incident in incidents:
        statuses[incident["status"]] += 1
        sources[incident.get("source") or "manual"] += 1
        events = store.list_events(incident["id"])
        created = incident["created_at"]

        started = ((incident.get("alert") or {}).get("started_at")) or None
        for name, value in (
            ("time_to_detect", _seconds(started, created)),
            (
                "time_to_diagnose",
                _seconds(
                    created, (_first(events, lambda e: e["kind"] == "diagnosis.completed") or {}).get("created_at")
                ),
            ),
            ("time_to_resolve", _seconds(created, (_entered(events, "resolved") or {}).get("created_at"))),
        ):
            if value is not None:
                stages[name].append(value)

        requested = _first(events, lambda e: e["kind"] == "approval.requested")
        human_decision = _first(
            events, lambda e: e["kind"] in ("approval.approved", "approval.rejected") and e.get("actor_type") == "human"
        )
        if requested and human_decision:
            stages["time_to_acknowledge"].append(_seconds(requested["created_at"], human_decision["created_at"]))
        remediating = _entered(events, "remediating")
        finished = _entered(events, "verifying") or _entered(events, "failed")
        if remediating and finished:
            stages["time_to_remediate"].append(_seconds(remediating["created_at"], finished["created_at"]))
        verifying = _entered(events, "verifying")
        verified = _first(events, lambda e: e["kind"] == "verification.completed")
        if verifying and verified:
            stages["time_to_verify"].append(_seconds(verifying["created_at"], verified["created_at"]))

        pending: dict[str, str] = {}
        for e in events:
            data = e.get("data") or {}
            if e["kind"] == "a2a.request":
                pending[data.get("agent", "?")] = e["created_at"]
            elif e["kind"] == "a2a.response" and e["actor"] in pending:
                agent_calls[e["actor"]] += 1
                agent_latency[e["actor"]].append(_seconds(pending.pop(e["actor"]), e["created_at"]))
            elif e["kind"] in ("agent.retry", "agent.unavailable", "contract.violation"):
                agent_failures[(data.get("agent") or "?", e["kind"])] += 1
            elif e["kind"] == "policy.evaluated":
                decisions[(data.get("decision") or {}).get("decision", "?")] += 1
            elif e["kind"] in ("alert.duplicate", "alert.correlated", "alert.resolved"):
                alerts[e["kind"].split(".")[1]] += 1

    closed = sum(statuses[s] for s in TERMINAL)
    agents = {}
    for agent in sorted(set(agent_calls) | {a for a, _ in agent_failures}):
        agents[agent] = {
            "calls": agent_calls[agent],
            "latency_seconds": summarize(agent_latency[agent]),
            "retries": agent_failures[(agent, "agent.retry")],
            "unavailable": agent_failures[(agent, "agent.unavailable")],
            "contract_violations": agent_failures[(agent, "contract.violation")],
        }
    return {
        "window": {"incidents": len(incidents), "limit": limit},
        "incidents": {
            "by_status": dict(statuses),
            "open": len(incidents) - closed,
            "resolved_rate": round(statuses["resolved"] / closed, 3) if closed else None,
            "escalated_rate": round(statuses["escalated"] / closed, 3) if closed else None,
            "by_source": dict(sources),
        },
        "stages": {name: summarize(stages[name]) for name in STAGES},
        "agents": agents,
        "policy_decisions": dict(decisions),
        "alerts": {
            "deduplicated": alerts["duplicate"],
            "correlated": alerts["correlated"],
            "resolved_noted": alerts["resolved"],
        },
    }
