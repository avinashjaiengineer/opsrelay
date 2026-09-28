# OpsRelay walkthrough

A hands-on tour: install OpsRelay, watch the agents resolve an incident in the dashboard, drive
the same flow from the CLI, and run the tests. Everything here runs offline on your own machine:
no AWS account, no Docker, no API key.

Want to see it before installing? The [user guide](USER_GUIDE.md) has the whole workflow in
screenshots.

## 1. Install

You need Python 3.11 or later.

**Windows (PowerShell):**

```powershell
cd C:\Users\<you>\Documents\opsrelay
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

**macOS / Linux:**

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Check it worked:

```powershell
opsrelay --help
```

> **If `opsrelay` or `pip` is blocked** (for example "An Application Control policy has blocked
> this file" on managed Windows PCs), use `python -m opsrelay` and `python -m pip` instead. They
> do the same thing. Every `opsrelay ...` command below also works as `python -m opsrelay ...`.

## 2. Start the platform

```powershell
opsrelay up
```

This starts six agents, each as its own A2A server, and opens the dashboard:

| Agent | Address |
|---|---|
| coordinator | http://127.0.0.1:8080/invocations |
| triage | http://127.0.0.1:9001 |
| diagnostics | http://127.0.0.1:9002 |
| remediation | http://127.0.0.1:9003 |
| verification | http://127.0.0.1:9004 |
| communications | http://127.0.0.1:9005 |

Your browser opens **http://127.0.0.1:8080/**. If it doesn't, open that link yourself. Keep this
terminal open; closing it (or pressing `Ctrl+C`) stops everything.

If port 8080 is in use: `opsrelay up --port 8090`, then open http://127.0.0.1:8090/.

## 3. Resolve an incident in the dashboard

1. **Enter your name** under **You (approver)**. Every decision is recorded in the audit log with
   this name.
2. **Click Bad deploy.** This injects a fault: a bad release of `checkout-api` pushes its error
   rate above 20%. In **Service health**, `checkout-api` turns *degraded*.
3. **Watch the lifecycle bar and the timeline.** The bar under the title shows where the
   incident is: open, triaging, investigating, awaiting approval, remediating, verifying,
   resolved. The coordinator hands the incident over A2A to:
   - **triage**, which submits a typed `TriageResult` (SEV1, because `checkout-api` is tier 1),
   - **diagnostics**, which submits a `DiagnosisResult`: root cause, evidence with sources and a
     confidence, shown under **Why the agents think this happened**,
   - **remediation**, which proposes `rollback_deployment` with a rollback plan.
4. **Read the policy decision.** The card under **Remediation · policy · approval** shows what the
   policy engine decided and why (for example *rollback_deployment always requires approval*,
   *high risk requires a person*). Nothing has touched the service yet.
5. **Decide.**
   - Click **Approve**: the platform runs the rollback once, the **verification** agent checks the
     service recovered, and communications posts updates and writes the **Postmortem**. Status
     ends at **resolved**.
   - Click **Reject** (optionally give a reason): nothing is executed, and the incident is
     **escalated** to the owning team.
6. **Tick "show tool calls"** to see every tool each agent called and its input. The green
   **audit chain verified** badge means no event in the incident's log has been altered.

Try **Traffic spike** too: scaling a tier-2 service is low risk, so the policy lets it run without
a person, and the agents resolve the incident on their own.

Try the other two scenarios the same way:

| Scenario | What breaks | Proposed fix |
|---|---|---|
| **Bad deploy** | `checkout-api` 5xx errors after a release | roll back the deployment |
| **Memory leak** | `auth-service` pods OOMKilled | restart the service |
| **Traffic spike** | `inventory-service` CPU saturated | scale out |

## 4. The same flow from the CLI

With `opsrelay up` still running, open a **second terminal**, activate the venv, and point the
CLI at the running coordinator:

```powershell
.venv\Scripts\Activate.ps1
$env:OPSRELAY_URL="http://127.0.0.1:8080"      # macOS / Linux: export OPSRELAY_URL=http://127.0.0.1:8080
```

```powershell
opsrelay simulate bad-deploy
```

```
inc-4cf88fa7c1: SEV1 on checkout-api. Root cause: Release 2.14.0 of checkout-api ... introduced an exception.
Waiting for human approval of rollback_deployment (apr-c746dc88f5, risk high).
```

Copy the `apr-...` id from your output (yours will differ), then approve it:

```powershell
opsrelay approvals                                   # list pending approvals
opsrelay approve apr-c746dc88f5 --by <your-name>     # or: opsrelay reject apr-... --by <you> --note "why"
```

```
inc-4cf88fa7c1 resolved. Postmortem written.
```

Then inspect the result:

```powershell
opsrelay show inc-4cf88fa7c1     # incident, approvals, full timeline and postmortem
opsrelay incidents               # every incident and its status
opsrelay health                  # all services back to OK
opsrelay audit inc-4cf88fa7c1    # verify the incident's tamper-evident audit chain
```

And look at the governance layer:

```powershell
opsrelay contracts                                            # lifecycle, agent contracts, who may make each move
opsrelay policy list                                          # the remediation policy in force
opsrelay policy test scale_service inventory-service --replicas 4    # ALLOW
opsrelay policy test delete_database payments-db                     # DENY: never allowed
opsrelay policy test restart_service auth-service --confidence 0.8   # APPROVAL_REQUIRED, with reasons
opsrelay dlq                                                  # requests to agents that stayed unavailable
```

The dashboard shows CLI-created incidents too, because both talk to the same coordinator.

**No server at all?** `opsrelay demo bad-deploy` runs the whole flow inside one process and asks
you to approve in the terminal.

## 5. Look at the A2A plumbing

While `opsrelay up` is running, open an agent's A2A card in your browser:

- http://127.0.0.1:9001/.well-known/agent-card.json (triage)
- http://127.0.0.1:9002/.well-known/agent-card.json (diagnostics)

The `a2a.request` and `a2a.response` rows in the timeline are the coordinator's real HTTP calls to
these servers.

## 6. Run the tests

```powershell
pytest                              # the whole suite, fully offline, about 15 seconds
pytest tests/test_runtime.py -v     # one file, with test names
ruff check . ; ruff format --check .
```

Expect every test to pass. A skip is normal when optional packages aren't installed; for
example, the infrastructure test needs `aws-cdk-lib` and `constructs`
(`python -m pip install aws-cdk-lib constructs`).

What the suite covers:

| File | Checks |
|---|---|
| `test_workflow.py` | the full incident lifecycle: approve, reject, policy-allowed, policy-denied, inconclusive |
| `test_lifecycle.py` | the state machine: exactly the allowed transitions, and who may make each |
| `test_contracts.py` | agent contracts: dispatch states, typed results, pydantic rejections, retries, dead letters |
| `test_policy.py` | policy decisions: risk, parameters, confidence thresholds, denials with reasons |
| `test_a2a.py` | coordinator to specialists over real A2A servers |
| `test_remote.py` | the A2A client used to reach AgentCore runtimes (SigV4) |
| `test_runtime.py` | the coordinator API contract, the CLI in `--url` mode, the dashboard |
| `test_approvals.py` | proposal, approval and execution engines; idempotent execution |
| `test_store.py` | SQLite and DynamoDB (moto) stores: compare-and-set status, hash chain, tamper detection |
| `test_agents.py` | agent tools and their guards |
| `test_infra.py` | the CDK stack synthesizes the expected AWS resources |

## 7. Next steps

- **Use a real model instead of the scripted one:** see "Using a real model on Bedrock locally"
  in the [README](../README.md#using-a-real-model-on-bedrock-locally). Amazon Nova is the
  default; Claude works too if your account has it.
- **Deploy to AWS:** see [Deploy to Amazon Bedrock AgentCore](../README.md#deploy-to-amazon-bedrock-agentcore).
- **How it's built:** [ARCHITECTURE.md](ARCHITECTURE.md).

## Troubleshooting

| Problem | Fix |
|---|---|
| `Activate.ps1 cannot be loaded` | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use `.venv\Scripts\activate.bat` from `cmd` |
| `opsrelay` / `pip` blocked by Application Control | Use `python -m opsrelay` / `python -m pip` |
| Port 8080 already in use | `opsrelay up --port 8090` |
| Dashboard says **disconnected** | The `opsrelay up` terminal was closed; start it again |
| CLI says it can't connect | Check `opsrelay up` is running and `OPSRELAY_URL` matches its port |
