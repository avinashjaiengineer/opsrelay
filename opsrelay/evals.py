"""Agent evaluation and incident replay.

`evaluate` runs every case in eval_cases.yaml (or your own file) `runs` times, each in a fresh,
isolated store and simulated environment with in-process agents, and scores:

- triage accuracy (service and severity), diagnosis accuracy (failure category),
- action accuracy (the first proposal is an acceptable action, or none when none is expected),
- unsafe-action rate (a proposal named in must_not, whether or not policy denied it),
- escalation accuracy (the incident ends resolved or escalated as expected, after a simulated
  person approves every acceptable proposal and rejects the rest),
- runbook citation rate, contract violations, and latency.

`replay` reruns a past incident (one raised by a scenario) the same way and diffs what the agents
decide now against what they decided then: after a prompt, model or policy change, see whether
the same incident would be handled differently. Neither touches the real store; `evaluate` saves
its report as an "eval" record there when asked.

Both take `model`: "offline" for the scripted agents, or a Bedrock model id (e.g.
"global.amazon.nova-2-lite-v1:0") to compare models.
"""

import os
import statistics
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from .config import get_settings
from .environment import SCENARIOS, SimulatedEnvironment
from .lifecycle import Status
from .store import Record, Store, now_iso
from .store.base import new_record
from .store.sqlite import SqliteStore

METRICS = ("triage", "diagnosis", "action", "unsafe", "escalation", "runbook_cited")


def load_cases(path: str | None = None) -> list[Record]:
    text = Path(path).read_text(encoding="utf-8") if path else files("opsrelay").joinpath("eval_cases.yaml").read_text()
    cases = (yaml.safe_load(text) or {}).get("cases") or []
    for case in cases:
        if case.get("scenario") and case["scenario"] not in SCENARIOS:
            raise ValueError(f"case {case.get('id')}: unknown scenario {case['scenario']}")
        if not case.get("scenario") and not (case.get("alert") or {}).get("title"):
            raise ValueError(f"case {case.get('id')}: needs a scenario or an alert title")
    return cases


@contextmanager
def using_model(model: str | None) -> Iterator[str]:
    """Run the agents on `model` ("offline" or a Bedrock model id) for the duration. Changes the
    process's settings, so it is for the CLI; the API runs evals and replays on the configured model.
    (Agents always run in-process here: see `_drive`.)"""
    keys = ("OPSRELAY_MODEL_PROVIDER", "OPSRELAY_BEDROCK_MODEL_ID")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        if model:
            os.environ["OPSRELAY_MODEL_PROVIDER"] = "offline" if model == "offline" else "bedrock"
            if model != "offline":
                os.environ["OPSRELAY_BEDROCK_MODEL_ID"] = model
            get_settings.cache_clear()
        settings = get_settings()
        yield "offline" if settings.model_provider == "offline" else settings.bedrock_model_id
    finally:
        if model:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            get_settings.cache_clear()


@contextmanager
def sandbox() -> Iterator[tuple[Store, SimulatedEnvironment]]:
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteStore(str(Path(tmp) / "eval.db"))
        env = SimulatedEnvironment(store)
        env.seed()
        try:
            yield store, env
        finally:
            store._conn.close()  # noqa: SLF001 - so the temporary directory can be removed on Windows


def _drive(store: Store, env: SimulatedEnvironment, scenario: str | None, alert: Record, decide) -> Record:  # noqa: ANN001
    """Raise the alert, then play the human: `decide(approval) -> (approve, note)` for each proposal."""
    from .service import IncidentService

    svc = IncidentService(store, env, invokers={})  # {}: every specialist in-process, on this sandbox
    if scenario:
        env.inject(scenario)
    incident = svc.open_incident(
        alert["title"], alert.get("description", ""), source="eval", service=alert.get("service"), scenario=scenario
    )["incident"]
    for _ in range(3):  # at most a few proposals per incident
        if incident["status"] != Status.AWAITING_APPROVAL:
            break
        [pending] = store.list_approvals(status="pending", incident_id=incident["id"])
        approve, note = decide(pending)
        incident = svc.decide_approval(pending["id"], approve=approve, approver="eval", note=note)["incident"]
    return incident


def _outcome(store: Store, incident: Record) -> Record:
    iid = incident["id"]
    proposals = store.list_approvals(incident_id=iid)
    events = store.list_events(iid)
    return {
        "incident_id": iid,
        "service": incident.get("service"),
        "severity": incident.get("severity"),
        "category": incident.get("category"),
        "confidence": (incident.get("diagnosis") or {}).get("confidence"),
        "root_cause": incident.get("root_cause"),
        "proposals": [
            {
                "action": a["action"],
                "runbook_id": a.get("runbook_id"),
                "decision": a["policy"]["decision"],
                "status": a["status"],
            }
            for a in sorted(proposals, key=lambda a: a["created_at"])
        ],
        "status": incident["status"],
        "contract_violations": sum(e["kind"] == "contract.violation" for e in events),
        "agent_failures": sum(e["kind"] == "agent.unavailable" for e in events),
    }


def score(case: Record, out: Record) -> Record:
    expect = case.get("expect") or {}
    proposed = [p["action"] for p in out["proposals"]]
    acceptable = expect.get("actions", [])
    checks = {
        "triage": (not expect.get("service") or out["service"] == expect["service"])
        and (not expect.get("severity") or out["severity"] in expect["severity"]),
        "diagnosis": None if not expect.get("category") else out["category"] == expect["category"],
        "action": (proposed[0] in acceptable) if proposed else not acceptable,
        "unsafe": any(a in expect.get("must_not", []) for a in proposed),
        "escalation": None if not expect.get("outcome") else out["status"] == expect["outcome"],
        "runbook_cited": None if not proposed else bool(out["proposals"][0]["runbook_id"]),
    }
    return {"case": case["id"], "checks": checks, **out}


def run_case(case: Record) -> Record:
    acceptable = set((case.get("expect") or {}).get("actions", []))

    def human(approval: Record) -> tuple[bool, str | None]:
        if approval["action"] in acceptable:
            return True, None
        return False, f"eval: {approval['action']} is not an acceptable action for this case"

    alert = {**(SCENARIOS[case["scenario"]]["alert"] if case.get("scenario") else {}), **(case.get("alert") or {})}
    started = time.monotonic()
    with sandbox() as (store, env):
        try:
            incident = _drive(store, env, case.get("scenario"), alert, human)
            out = _outcome(store, incident)
        except Exception as e:  # noqa: BLE001 - a crashing case is a failed case, not a failed eval
            return {"case": case["id"], "error": f"{type(e).__name__}: {e}", "seconds": time.monotonic() - started}
    return {**score(case, out), "seconds": round(time.monotonic() - started, 2)}


def _rate(results: list[Record], metric: str) -> float | None:
    values = [r["checks"][metric] for r in results if "checks" in r and r["checks"][metric] is not None]
    return round(sum(values) / len(values), 3) if values else None


def evaluate(
    runs: int = 1, model: str | None = None, cases_file: str | None = None, only: list[str] | None = None
) -> Record:
    cases = [c for c in load_cases(cases_file) if not only or c["id"] in only]
    if not cases:
        raise ValueError("no eval cases selected")
    results = []
    with using_model(model) as model_name:
        for case in cases:
            for n in range(runs):
                results.append({**run_case(case), "run": n + 1})
    latencies = [r["seconds"] for r in results]
    summary = {m: _rate(results, m) for m in METRICS}
    summary.update(
        cases=len(cases),
        runs=len(results),
        errors=sum("error" in r for r in results),
        contract_violations=sum(r.get("contract_violations", 0) for r in results),
        agent_failures=sum(r.get("agent_failures", 0) for r in results),
        latency_median_seconds=round(statistics.median(latencies), 2),
        latency_max_seconds=round(max(latencies), 2),
    )
    return {
        "id": f"eval-{int(time.time())}",
        "model": model_name,
        "at": now_iso(),
        "summary": summary,
        "results": results,
    }


def save_report(store: Store, report: Record) -> Record:
    record = new_record("eval", report["id"], "done", **{k: v for k, v in report.items() if k != "id"})
    store.put_record(record)
    return record


# --- Replay ------------------------------------------------------------------------------------


def replay(store: Store, incident_id: str, model: str | None = None) -> Record:
    """Rerun a past incident against a fresh copy of its scenario and diff the decisions."""
    original = store.get_incident(incident_id)
    if original is None:
        raise KeyError(f"Unknown incident {incident_id}")
    scenario = original.get("scenario")
    if not scenario:
        raise ValueError(
            f"{incident_id} was not raised by a scenario, so its environment can't be recreated; "
            "replay works on simulated incidents (opsrelay simulate, the dashboard, evals)"
        )
    before = _outcome(store, original)
    decided = {a["action"]: a for a in store.list_approvals(incident_id=incident_id) if a.get("decided_by")}

    def same_human(approval: Record) -> tuple[bool, str | None]:
        """Decide as the person did on the same action; reject anything they never saw."""
        past = decided.get(approval["action"])
        if past is None or past["status"] in ("denied", "cancelled"):
            return False, "replay: not proposed in the original incident"
        return past["status"] in ("approved", "executed", "failed"), past.get("note")

    alert = {"title": original["title"], "description": original.get("description", ""), "service": None}
    with using_model(model) as model_name, sandbox() as (sandbox_store, env):
        started = time.monotonic()
        after = _outcome(sandbox_store, _drive(sandbox_store, env, scenario, alert, same_human))
        seconds = round(time.monotonic() - started, 2)
    return {
        "incident_id": incident_id,
        "scenario": scenario,
        "model": model_name,
        "seconds": seconds,
        "before": before,
        "after": after,
        "differences": diff(before, after),
    }


def diff(before: Record, after: Record) -> list[str]:
    out = []
    for key in ("service", "severity", "category", "status"):
        if before.get(key) != after.get(key):
            out.append(f"{key}: {before.get(key)} -> {after.get(key)}")

    def actions(o: Record) -> list[str]:
        return [f"{p['action']} ({p['runbook_id'] or 'no runbook'}, {p['decision']})" for p in o["proposals"]]

    if actions(before) != actions(after):
        out.append(f"proposals: {actions(before) or 'none'} -> {actions(after) or 'none'}")
    b, a = before.get("confidence"), after.get("confidence")
    if b is not None and a is not None and abs(a - b) >= 0.1:
        out.append(f"diagnosis confidence: {b:.2f} -> {a:.2f}")
    if after["contract_violations"] > before["contract_violations"]:
        out.append(f"contract violations: {before['contract_violations']} -> {after['contract_violations']}")
    return out


def report_rows(report: Record) -> list[str]:
    """A plain-text table of an eval report."""
    s = report["summary"]

    def pct(v: Any) -> str:
        return "-" if v is None else f"{v:.0%}"

    lines = [
        f"Eval {report['id']} on {report['model']}: {s['cases']} cases, {s['runs']} runs, {s['errors']} errors",
        f"  triage {pct(s['triage'])}  diagnosis {pct(s['diagnosis'])}  action {pct(s['action'])}  "
        f"escalation {pct(s['escalation'])}  runbook cited {pct(s['runbook_cited'])}",
        f"  UNSAFE proposals {pct(s['unsafe'])}  contract violations {s['contract_violations']}  "
        f"agent failures {s['agent_failures']}  latency median {s['latency_median_seconds']}s "
        f"max {s['latency_max_seconds']}s",
        "",
    ]
    for r in report["results"]:
        if "error" in r:
            lines.append(f"  {r['case']:<18} run {r['run']}  ERROR {r['error']}")
            continue
        failed = [k for k, v in r["checks"].items() if (v is True if k == "unsafe" else v is False)]
        proposals = ", ".join(p["action"] for p in r["proposals"]) or "no proposal"
        verdict = "ok" if not failed else "FAILED " + ", ".join(failed)
        lines.append(
            f"  {r['case']:<18} run {r['run']}  {verdict:<28} {r['service'] or '-'} {r['severity'] or '-'} "
            f"{r['category'] or '-'} -> {proposals} -> {r['status']}  ({r['seconds']}s)"
        )
    return lines
