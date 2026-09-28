"""Command line: run OpsRelay locally, or drive the coordinator deployed on AgentCore.

    opsrelay demo                         # full offline run of a scenario, approval prompt included
    opsrelay simulate bad-deploy          # inject a fault and let the agents respond
    opsrelay open "title" -d "details"    # open an incident by hand
    opsrelay approvals                    # pending approvals
    opsrelay approve apr-123 --by jane    # approve and let the agents continue
    opsrelay reject apr-123 --by jane --note "not during peak"
    opsrelay show inc-123                 # incident, approvals and timeline
    opsrelay incidents | health
    opsrelay audit inc-123                # verify the incident's tamper-evident audit chain
    opsrelay dlq                          # requests to agents that stayed unavailable
    opsrelay policy list                  # the remediation policy in force
    opsrelay policy test scale_service inventory-service --replicas 4
    opsrelay contracts                    # lifecycle states, agent contracts, who may make each move
    opsrelay whoami                       # who you are to the coordinator, and your roles
    opsrelay policy propose new.yaml      # then: policy approve v2 (a second admin), policy activate v2
    opsrelay users add "Jane" --roles sre,incident_commander   # dev-mode users; prints a token once
    opsrelay up                           # run all six agents locally + a dashboard at http://127.0.0.1:8080

By default commands run the agents inside this process. To send them to a running coordinator:
    --url http://127.0.0.1:8080            # one started with `opsrelay up` (or OPSRELAY_URL)
    --remote <coordinator runtime ARN>     # the one deployed on AgentCore (or OPSRELAY_COORDINATOR_ARN)
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
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


def _http_call(url: str, payload: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = httpx.post(url.rstrip("/") + "/invocations", json=payload, headers=headers, timeout=900)
    if resp.status_code in (401, 403):
        return (
            resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"error": resp.text}
        )
    resp.raise_for_status()
    return resp.json()


def _add_user(path: str, name: str, roles: list[str]) -> int:
    """Create a dev user with a new random token; print the token once, store only its hash."""
    import secrets as pysecrets

    import yaml

    from .auth import ROLES, token_hash

    unknown = set(roles) - set(ROLES)
    if unknown:
        print(f"error: unknown roles {sorted(unknown)}; choose from {', '.join(ROLES)}", file=sys.stderr)
        return 1
    file = Path(path)
    doc = (yaml.safe_load(file.read_text(encoding="utf-8")) if file.exists() else None) or {}
    users = [u for u in doc.get("users") or [] if u.get("name") != name]
    token = pysecrets.token_urlsafe(32)
    users.append({"name": name, "roles": roles, "token_sha256": token_hash(token)})
    file.write_text(yaml.safe_dump({"users": users}, sort_keys=False), encoding="utf-8")
    print(f"Added {name} ({', '.join(roles)}) to {path}.")
    print(f"Token (shown once; give it to {name}, it isn't stored): {token}")
    return 0


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
        decision = (a.get("policy") or {}).get("decision", "")
        print(
            f"  approval {a['id']}: {a['action']} {a['service']} {a['params'] or ''} risk={a['risk']} "
            f"policy={decision} -> {a['status']}"
        )
    for e in data.get("events", []):
        if e["kind"] not in ("tool.invoked", "tool.completed"):
            print(f"  {e['created_at'][11:19]} {e['actor']:<22} {e['kind']:<24} {e['message'][:110]!s}".rstrip())
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
    parser.add_argument(
        "--token",
        default=os.environ.get("OPSRELAY_TOKEN"),
        help="bearer token for a coordinator with authentication on (or OPSRELAY_TOKEN)",
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
        p.add_argument("--by", help="who is deciding (needed only with authentication off; else it's you)")
        p.add_argument("--note")
    p = sub.add_parser("show", help="show an incident and its timeline")
    p.add_argument("incident_id")
    sub.add_parser("incidents", help="list incidents")
    sub.add_parser("health", help="service health")
    p = sub.add_parser("audit", help="verify an incident's audit hash chain")
    p.add_argument("incident_id")
    sub.add_parser("dlq", help="list dead letters (requests to agents that stayed unavailable)")
    p = sub.add_parser("policy", help="show or test the remediation policy")
    policy_sub = p.add_subparsers(dest="policy_cmd", required=True)
    policy_sub.add_parser("list", help="show the policy in force")
    policy_sub.add_parser("history", help="policy versions: proposed, approved, active, superseded")
    p = policy_sub.add_parser("propose", help="propose a new policy version from a YAML file")
    p.add_argument("file")
    p.add_argument("--note", default="")
    p.add_argument("--by", help="needed only with authentication off")
    for name, help_text in (
        ("approve", "approve a proposed version (not your own)"),
        ("reject", "reject a proposed version"),
    ):
        p = policy_sub.add_parser(name, help=help_text)
        p.add_argument("version")
        p.add_argument("--note", default="")
        p.add_argument("--by", help="needed only with authentication off")
    p = policy_sub.add_parser("activate", help="put an approved version in force")
    p.add_argument("version")
    p.add_argument("--by", help="needed only with authentication off")
    p = policy_sub.add_parser("test", help="the decision for a proposal, without recording anything")
    p.add_argument("action_name", metavar="ACTION")
    p.add_argument("service")
    p.add_argument("--replicas", type=int)
    p.add_argument("--target-version")
    p.add_argument("--confidence", type=float, default=0.95, help="diagnosis confidence (default 0.95)")
    sub.add_parser("contracts", help="lifecycle states, agent contracts and who may make each move")
    sub.add_parser("whoami", help="who the coordinator thinks you are, and your roles")
    p = sub.add_parser("users", help="manage dev-mode users (OPSRELAY_AUTH_MODE=dev)")
    users_sub = p.add_subparsers(dest="users_cmd", required=True)
    p = users_sub.add_parser("add", help="add a user with a new token (printed once)")
    p.add_argument("name")
    p.add_argument("--roles", required=True, help="comma-separated, e.g. sre,incident_commander")
    p.add_argument("--file", default=os.environ.get("OPSRELAY_DEV_USERS") or "users.yaml")
    p = sub.add_parser("up", help="run the coordinator and the five specialists locally over A2A (no Docker)")
    p.add_argument("--port", type=int, default=8080, help="coordinator port (default 8080)")
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="where the coordinator and dashboard listen (default 127.0.0.1; 0.0.0.0 for a server)",
    )
    p.add_argument("--specialist-port", type=int, default=9001, help="first of five specialist ports (default 9001)")
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

    if args.cmd == "users":
        return _add_user(args.file, args.name, [r.strip() for r in args.roles.split(",") if r.strip()])

    def call(payload: dict[str, Any]) -> dict[str, Any]:
        if args.remote:
            result = _remote_call(args.remote, payload)
        elif args.url:
            result = _http_call(args.url, payload, args.token)
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
    elif args.cmd == "whoami":
        payload = {"action": "whoami"}
    elif args.cmd == "show":
        payload = {"action": "get_incident", "incident_id": args.incident_id}
    elif args.cmd == "incidents":
        payload = {"action": "list_incidents"}
    elif args.cmd == "audit":
        payload = {"action": "verify_audit", "incident_id": args.incident_id}
    elif args.cmd == "dlq":
        payload = {"action": "list_dead_letters"}
    elif args.cmd == "policy" and args.policy_cmd == "list":
        payload = {"action": "get_policy"}
    elif args.cmd == "policy" and args.policy_cmd == "history":
        payload = {"action": "list_policies"}
    elif args.cmd == "policy" and args.policy_cmd == "propose":
        payload = {
            "action": "propose_policy",
            "text": Path(args.file).read_text(encoding="utf-8"),
            "note": args.note,
            "by": args.by,
        }
    elif args.cmd == "policy" and args.policy_cmd in ("approve", "reject"):
        payload = {
            "action": "review_policy",
            "version": args.version,
            "approve": args.policy_cmd == "approve",
            "note": args.note,
            "by": args.by,
        }
    elif args.cmd == "policy" and args.policy_cmd == "activate":
        payload = {"action": "activate_policy", "version": args.version, "by": args.by}
    elif args.cmd == "policy":
        parameters: dict[str, Any] = {}
        if args.replicas is not None:
            parameters["replicas"] = args.replicas
        if args.target_version:
            parameters["target_version"] = args.target_version
        payload = {
            "action": "test_policy",
            "action_name": args.action_name,
            "service": args.service,
            "parameters": parameters,
            "confidence": args.confidence,
        }
    elif args.cmd == "contracts":
        payload = {"action": "get_contracts"}
    else:
        payload = {"action": "health"}

    result = call(payload)
    if args.json or args.cmd in ("contracts",) or (args.cmd == "policy" and args.policy_cmd == "list"):
        print(json.dumps(result, indent=2, default=str))
    elif args.cmd == "whoami":
        who = result["principal"]
        verified = "verified" if who["verified"] else "NOT verified"
        print(f"{who['name']}  roles: {', '.join(who['roles'])}  ({who['method']}, {verified})")
    elif args.cmd == "policy" and args.policy_cmd == "history":
        for v in result["policies"] or []:
            reviewer = v.get("reviewed_by") or "-"
            print(f"{v['id']:<5} {v['status']:<11} by {v['author']}  reviewed by {reviewer}  {v.get('note') or ''}")
        if not result["policies"]:
            print("No stored versions; the file policy is in force.")
    elif args.cmd == "policy" and args.policy_cmd != "test":
        v = result["policy"]
        print(f"{v['id']}: {v['status']}")
    elif args.cmd == "audit":
        if result["ok"]:
            print(
                f"{result['incident_id']}: audit chain intact ({result['events']} events, head {result['head'][:16]})"
            )
        else:
            print(
                f"{result['incident_id']}: TAMPERED at event #{result['at']} ({result['event_id']}): {result['reason']}"
            )
            return 2
    elif args.cmd == "dlq":
        if not result["dead_letters"]:
            print("No dead letters.")
        for d in result["dead_letters"]:
            print(f"{d['id']}  {d['incident_id']}  {d['agent']}  {d['created_at']}  {d['error']}")
    elif args.cmd == "policy":
        d = result["decision"]
        print(f"{d['decision']}  risk={d['risk']}  requires_human={d['requires_human']}  ({d['policy_version']})")
        for reason in d["reasons"]:
            print(f"  - {reason}")
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
    for reason in (a.get("policy") or {}).get("reasons", []):
        print(f"   Policy: {reason}")
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
