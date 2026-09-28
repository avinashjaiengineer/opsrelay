"""Command line: run OpsRelay locally, or drive the coordinator deployed on AgentCore.

    opsrelay demo                         # full offline run of a scenario, approval prompt included
    opsrelay simulate bad-deploy          # inject a fault and let the agents respond
    opsrelay open "title" -d "details"    # open an incident by hand
    opsrelay approvals                    # pending approvals
    opsrelay approve apr-123 --by jane    # approve and let the agents continue
    opsrelay reject apr-123 --by jane --note "not during peak"
    opsrelay show inc-123                 # incident, approvals and timeline
    opsrelay incidents | health
    opsrelay up                           # run all five agents locally + a dashboard at http://127.0.0.1:8080

By default commands run the agents inside this process. To send them to a running coordinator:
    --url http://127.0.0.1:8080            # one started with `opsrelay up` (or OPSRELAY_URL)
    --remote <coordinator runtime ARN>     # the one deployed on AgentCore (or OPSRELAY_COORDINATOR_ARN)
"""

import argparse
import json
import os
import sys
import uuid
from typing import Any

from .environment import SCENARIOS


def _remote_call(arn: str, payload: dict[str, Any]) -> dict[str, Any]:
    import boto3

    region = arn.split(":")[3]
    client = boto3.client("bedrock-agentcore", region_name=region)
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=f"opsrelay-cli-{uuid.uuid4().hex}",
        contentType="application/json",
        accept="application/json",
        payload=json.dumps(payload).encode(),
    )
    return json.loads(resp["response"].read())


def _http_call(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    import httpx

    resp = httpx.post(url.rstrip("/") + "/invocations", json=payload, timeout=900)
    resp.raise_for_status()
    return resp.json()


def _local_call(payload: dict[str, Any]) -> dict[str, Any]:
    from .runtime.coordinator import invoke

    return invoke(payload)


def _print_incident(data: dict[str, Any]) -> None:
    inc = data["incident"]
    print(f"{inc['id']}  [{inc['status']}]  {inc.get('severity') or '-'}  {inc.get('service') or '-'}")
    print(f"  {inc['title']}")
    if inc.get("root_cause"):
        print(f"  Root cause: {inc['root_cause']}")
    for a in data.get("approvals", []):
        print(
            f"  approval {a['id']}: {a['action']} {a['service']} {a['params'] or ''} risk={a['risk']} -> {a['status']}"
        )
    for e in data.get("events", []):
        if e["kind"] != "tool.call":
            print(f"  {e['created_at'][11:19]} {e['actor']:<22} {e['kind']:<22} {e['message'][:110]!s}".rstrip())
    if inc.get("postmortem"):
        print("\n" + inc["postmortem"])


def _print_approvals(approvals: list[dict[str, Any]]) -> None:
    if not approvals:
        print("No approvals.")
    for a in approvals:
        print(f"{a['id']}  {a['incident_id']}  {a['action']} on {a['service']} {a['params'] or ''}  risk={a['risk']}")
        print(f"    {a['rationale']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="opsrelay", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--remote", default=os.environ.get("OPSRELAY_COORDINATOR_ARN"), help="coordinator runtime ARN")
    parser.add_argument(
        "--url", default=os.environ.get("OPSRELAY_URL"), help="coordinator URL, e.g. from `opsrelay up`"
    )
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("demo", help="offline end-to-end demo with an approval prompt")
    p.add_argument("scenario", nargs="?", default="bad-deploy", choices=sorted(SCENARIOS))
    p.add_argument("--yes", action="store_true", help="approve without prompting")

    p = sub.add_parser("simulate", help="inject a fault and open its incident")
    p.add_argument("scenario", choices=sorted(SCENARIOS))
    p = sub.add_parser("open", help="open an incident")
    p.add_argument("title")
    p.add_argument("-d", "--description", default="")
    p.add_argument("--service")
    p = sub.add_parser("approvals", help="list approvals")
    p.add_argument("--status", default="pending", help="pending, executed, rejected, failed, or all")
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} a proposed action")
        p.add_argument("approval_id")
        p.add_argument("--by", required=True, help="who is deciding (recorded in the audit log)")
        p.add_argument("--note")
    p = sub.add_parser("show", help="show an incident and its timeline")
    p.add_argument("incident_id")
    sub.add_parser("incidents", help="list incidents")
    sub.add_parser("health", help="service health")
    p = sub.add_parser("up", help="run the coordinator and the four specialists locally over A2A (no Docker)")
    p.add_argument("--port", type=int, default=8080, help="coordinator port (default 8080)")
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="where the coordinator and dashboard listen (default 127.0.0.1; 0.0.0.0 for a server)",
    )
    p.add_argument("--specialist-port", type=int, default=9001, help="first of four specialist ports (default 9001)")
    p.add_argument("--no-browser", action="store_true", help="don't open the dashboard in a browser")

    args = parser.parse_args(argv)

    if args.cmd == "up":
        from .local import run_local_stack

        run_local_stack(
            host=args.host,
            port=args.port,
            specialist_base_port=args.specialist_port,
            open_browser=not args.no_browser,
        )
        return 0

    def call(payload: dict[str, Any]) -> dict[str, Any]:
        if args.remote:
            result = _remote_call(args.remote, payload)
        elif args.url:
            result = _http_call(args.url, payload)
        else:
            result = _local_call(payload)
        if "error" in result:
            print(f"error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        return result

    if args.cmd == "demo":
        return _demo(call, args.scenario, args.yes)

    payload: dict[str, Any]
    if args.cmd == "simulate":
        payload = {"action": "simulate", "scenario": args.scenario}
    elif args.cmd == "open":
        payload = {
            "action": "open_incident",
            "title": args.title,
            "description": args.description,
            "service": args.service,
        }
    elif args.cmd == "approvals":
        payload = {"action": "list_approvals", "status": None if args.status == "all" else args.status}
    elif args.cmd in ("approve", "reject"):
        payload = {
            "action": "decide_approval",
            "approval_id": args.approval_id,
            "approve": args.cmd == "approve",
            "approver": args.by,
            "note": args.note,
        }
    elif args.cmd == "show":
        payload = {"action": "get_incident", "incident_id": args.incident_id}
    elif args.cmd == "incidents":
        payload = {"action": "list_incidents"}
    else:
        payload = {"action": "health"}

    result = call(payload)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    elif args.cmd == "approvals":
        _print_approvals(result["approvals"])
    elif args.cmd == "incidents":
        for inc in result["incidents"]:
            print(f"{inc['id']}  [{inc['status']}]  {inc.get('severity') or '-'}  {inc['title']}")
    elif args.cmd == "health":
        for s in result["services"]:
            print(
                f"{s['service']:<20} tier {s['tier']}  {'OK ' if s['healthy'] else 'BAD'}  "
                f"errors {s['error_rate']:.1%}  p99 {s['p99_latency_ms']} ms"
            )
    elif args.cmd == "show":
        _print_incident(result)
    else:
        if result.get("report"):
            print(result["report"])
        inc_id = (result.get("incident") or {}).get("id")
        if inc_id:
            _print_incident(call({"action": "get_incident", "incident_id": inc_id}))
    return 0


def _demo(call, scenario: str, yes: bool) -> int:  # noqa: ANN001
    print(f"== Injecting scenario '{scenario}' and raising its alert\n")
    result = call({"action": "simulate", "scenario": scenario})
    print(f"Coordinator: {result['report']}\n")
    incident_id = result["incident"]["id"]
    pending = call({"action": "list_approvals", "status": "pending"})["approvals"]
    pending = [a for a in pending if a["incident_id"] == incident_id]
    if not pending:
        _print_incident(call({"action": "get_incident", "incident_id": incident_id}))
        return 0
    a = pending[0]
    print(f"== Human approval needed: {a['action']} on {a['service']} {a['params'] or ''} (risk {a['risk']})")
    print(f"   Rationale: {a['rationale']}")
    approve = yes or input("   Approve? [y/N] ").strip().lower() in ("y", "yes")
    decided = call(
        {
            "action": "decide_approval",
            "approval_id": a["id"],
            "approve": approve,
            "approver": os.environ.get("USER") or os.environ.get("USERNAME") or "demo-user",
            "note": None if approve else "rejected in demo",
        }
    )
    print(f"\nCoordinator: {decided['report']}\n")
    _print_incident(call({"action": "get_incident", "incident_id": incident_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
