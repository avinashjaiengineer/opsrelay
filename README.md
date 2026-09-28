# OpsRelay

[![CI](https://github.com/avinashjaiengineer/opsrelay/actions/workflows/ci.yml/badge.svg)](https://github.com/avinashjaiengineer/opsrelay/actions/workflows/ci.yml)

**An agent-to-agent platform for enterprise IT incident resolution, built on [Strands Agents](https://strandsagents.com) and hosted on [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/).**

When an alert fires, a coordinator agent hands the incident to specialist agents over the
**A2A (Agent2Agent) protocol**:

- the **triage** agent sets severity,
- the **diagnostics** agent finds the root cause, with evidence and a confidence,
- the **remediation** agent proposes a runbook fix (it can never execute one),
- the **verification** agent checks that the fix worked,
- the **communications** agent updates stakeholders and writes the postmortem.

Agents only propose. A **policy engine** decides what may run and who must approve it, a person
approves, and only then does the platform execute, once. Every incident follows a strict **state
machine**, every agent has a **typed contract** the platform enforces, and the audit log is a
**tamper-evident hash chain**.

The agents work from your **runbooks** (retrieved with Amazon Titan embeddings) and from **incident
memory** (how similar incidents went, including fixes people rejected). An **evaluation suite** and
**incident replay** measure them before a model or prompt change ships, and **Slack, Teams,
PagerDuty and Jira** hear about incidents as they happen (Slack can approve from the message).

![OpsRelay dashboard: an alert fires, the agents investigate, a person approves, the incident is resolved](docs/images/workflow.gif)

See the [user guide](docs/USER_GUIDE.md) for the whole workflow in screenshots, why it's useful,
and ideas for improving it.

**It fixes real outages.** Below, a bad release on a real ECS service: the CloudWatch alarm fires,
the agents diagnose it from real metrics, logs and the deployment, a person approves in Slack, and
OpsRelay rolls back and verifies recovery on live metrics, about seven minutes end to end
([write-up](docs/WRITEUP.md); try it with [deploy/demo](deploy/demo/demo.py)).

![A real ECS outage handled end to end](docs/images/real-outage.gif)

**Measured, not assumed.** On Amazon Nova 2 Lite, the evaluation suite found the agents proposing
the right fix only half the time. Hardening the platform against what it found (stalls, retry
loops, prose instead of typed results, an injected instruction relayed between agents) took that to
91% over 12 runs and 100% on a rerun of the failed cases, with no unsafe proposal in any run.
[Details](docs/USER_GUIDE.md#12-measuring-the-agents).

```
 alert / API / CLI
        |
        v
+------------------+   A2A (JSON-RPC, SigV4)   +------------------+
|   coordinator    |-------------------------->|  triage          |
| AgentCore Runtime|-------------------------->|  diagnostics     |  5 AgentCore Runtimes
|  (HTTP protocol) |-------------------------->|  remediation     |  (A2A protocol)
|                  |-------------------------->|  verification    |
|                  |-------------------------->|  communications  |
+--------+---------+                           +--------+---------+
         |   proposal -> policy engine -> approval -> executor (once, idempotent)
         v                                              v
+--------------------------------------------------------------------------+
| DynamoDB: incidents (state machine) . approvals . hash-chained audit log |
+--------------------------------------------------------------------------+
         ^
         |  approve / reject  (a person, via dashboard, CLI or API)
```

## How an incident flows

Every incident moves through one state machine (`opsrelay/lifecycle.py`). No other move is
possible: agents, tools and the platform all change status through one function that checks the
move, checks that the actor may make it, and writes it atomically.

```
OPEN -> TRIAGING -> INVESTIGATING -> AWAITING_APPROVAL -> REMEDIATING -> VERIFYING -> RESOLVED
           |              |                 |                                  |
           +--------------+-----------------+--> ESCALATED <-- FAILED <--------+
                                                      (REMEDIATING -> FAILED too)
```

1. **Alert in** (`open`). From an alert (`simulate`), the API (`open_incident`) or the CLI.
2. **Triage** (`triaging`). Triage submits a `TriageResult`: service, severity, customer impact, confidence.
3. **Diagnose** (`investigating`). Diagnostics submits a `DiagnosisResult`: root cause, evidence with sources, affected component, confidence.
4. **Propose.** Remediation searches the runbooks and similar past incidents, then submits a `RemediationProposal`: action, parameters, its own risk estimate, rollback plan, and the runbook it follows.
5. **Policy** (`awaiting_approval`). The policy engine (`opsrelay/policies.yaml`) returns ALLOW, APPROVAL_REQUIRED or DENY, with reasons. Diagnosis confidence below 0.70 is denied; below 0.90 always needs a person; so does an action the cited runbook doesn't recommend.
6. **Human gate.** A person approves or rejects. A rejection **escalates** the incident.
7. **Execute** (`remediating`). The execution engine runs the action once, keyed by an idempotency key.
8. **Verify** (`verifying`). The verification agent, not the one that proposed the fix, checks recovery; the platform cross-checks its claim against live metrics. No recovery means `failed`, then `escalated`.
9. **Close** (`resolved`). Communications posts internal and customer updates, then submits a typed `Postmortem`. The incident is written to incident memory, and Slack, Teams, PagerDuty and Jira are updated.

If an agent stays unavailable, calls are retried with backoff behind a circuit breaker; then the
request goes to a dead-letter queue and the incident to a person.

## Quick start (no AWS account, no Docker)

New here? [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) takes you step by step through installing,
resolving an incident in the dashboard and from the CLI, and running the tests.

You only need Python 3.11 or later. Offline mode runs the same Strands agents, tools and A2A
wiring with a scripted model instead of an LLM.

**Windows (PowerShell):**

```powershell
cd C:\Users\<you>\Documents\opsrelay
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
opsrelay demo bad-deploy
```

If PowerShell refuses to run `Activate.ps1`, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use `.venv\Scripts\activate.bat` from `cmd`.

**macOS / Linux:**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
opsrelay demo bad-deploy
```

The demo shows the agents triage, diagnose and propose a rollback, then prompts you to approve
it. After you approve, the agents verify recovery and print the postmortem. The other scenarios
are `memory-leak` and `traffic-spike`.

### The dashboard: watch the agents work in your browser

```powershell
opsrelay up
```

This starts the five specialist agents as separate A2A servers (ports 9001-9005) and the
coordinator on port 8080, then opens **http://127.0.0.1:8080/** in your browser. Keep the
terminal open while you use it.

In the dashboard:

1. Enter your name under **You (approver)**. It is recorded in the audit log.
2. Click a scenario, such as **Bad deploy**. The incident appears, and the timeline fills in as
   the coordinator delegates to triage, diagnostics and remediation over A2A.
3. When a **Human approval** card appears, click **Approve** or **Reject**.
4. Watch the agents verify the fix, update stakeholders and write the postmortem. Tick
   **show tool calls** to see every tool call the agents made.

If port 8080 is taken, run `opsrelay up --port 8090` and open http://127.0.0.1:8090/.
To see an agent's A2A card, open http://127.0.0.1:9001/.well-known/agent-card.json.

You can also drive the running stack from a second terminal (activate the venv first):

```powershell
$env:OPSRELAY_URL="http://127.0.0.1:8080"
opsrelay simulate bad-deploy
opsrelay approvals
opsrelay approve apr-xxxxxxxxxx --by you
opsrelay show inc-xxxxxxxxxx
```

### Using a real model on Bedrock locally

Set these, plus AWS credentials (for example `aws configure`), then run `demo` or `up` as above:

```powershell
$env:OPSRELAY_MODEL_PROVIDER="bedrock"
$env:OPSRELAY_BEDROCK_MODEL_ID="global.amazon.nova-2-lite-v1:0"   # see "Model id" below
$env:OPSRELAY_AWS_REGION="us-east-1"
```

On macOS / Linux, use `export NAME=value` instead of `$env:NAME="value"`.

### Optional: Docker Compose

If you have Docker, `docker compose up --build` runs the same topology in containers with DynamoDB Local.

## Run on a single EC2 instance

[deploy/ec2/user-data.sh](deploy/ec2/user-data.sh) installs OpsRelay as a systemd service
(`opsrelay up`) with Amazon Nova on Bedrock. For data that survives the instance and a dashboard on
HTTPS:

```
 you (browser, CLI) --HTTPS--> API Gateway (HTTP API) --adds origin secret--> EC2 :8080
                                                                              opsrelay up
 CloudWatch alarm --> EventBridge --> SQS (+DLQ) ------------ intake -------> | coordinator + 5 specialists (A2A)
                                                                              | job worker, recovery
 Slack  <---- messages (bot token) ------------------------------------------ | notification outbox
        ---- Approve / Reject over Socket Mode (app token) -----------------> |
                                                                              v
       Amazon Bedrock: Nova 2 Lite (agents), Titan Text Embeddings v2 (runbooks, incident memory)
       DynamoDB: incidents, approvals, hash-chained audit log, memory, jobs (PITR on)
       Secrets Manager: user tokens, Slack tokens, origin secret        AWS Budgets: $25/month alert
```

1. **DynamoDB.** Copy the existing SQLite data (audit chains stay verifiable), then point the
   service at the table:
   ```bash
   opsrelay migrate --from /opt/opsrelay/opsrelay.db --to-table opsrelay    # creates the table if missing
   # systemd drop-in: OPSRELAY_STORE=dynamodb, OPSRELAY_DYNAMODB_TABLE=opsrelay
   ```
   Turn on point-in-time recovery and deletion protection on the table, and give the instance
   role item access to it (Get/Put/Update/Delete/Query/ConditionCheck on the table and its index).
2. **HTTPS.** Put an API Gateway HTTP API (an `HTTP_PROXY` integration to the instance's port 8080)
   or CloudFront in front, with an Elastic IP on the instance. Have the front door add an
   `x-opsrelay-origin` header with a random value, and set `OPSRELAY_ORIGIN_SECRET` to it (a Secrets
   Manager reference works): requests that bypass the front door are refused. Set
   `OPSRELAY_PUBLIC_URL` to the HTTPS address so Slack, Teams, PagerDuty and Jira link to it.
   API Gateway cuts requests off at 30 s; the dashboard uses `async` for anything longer.
3. **Slack.** Create the app from [deploy/slack-app-manifest.yaml](deploy/slack-app-manifest.yaml)
   with your HTTPS address; see Integrations below.
4. **A real workload.** `python deploy/demo/demo.py up` runs `shop-api` on ECS Fargate (about $0.30
   a day) with a CloudWatch alarm, and prints its service catalog entry. Run OpsRelay with
   `OPSRELAY_ENVIRONMENT=hybrid` (catalog services are real; the rest stay simulated, so scenarios
   still work) and `OPSRELAY_SERVICE_CATALOG`. `demo.py break` deploys a bad release; `down` removes
   everything.

## Deploy to Amazon Bedrock AgentCore

**Prerequisites:**

- An AWS account.
- Access to a tool-use model in Amazon Bedrock (Amazon Nova by default; see "Model id" below).
- Node.js (for the CDK CLI).
- Docker with `buildx`, **for deploying only**. `cdk deploy` builds the agents' container
  image. AgentCore runs **linux/arm64** containers, and the stack builds for arm64 automatically.
  Without Docker on your machine, run the deploy from a machine or CI runner that has it, such as
  a GitHub Actions `ubuntu-24.04-arm` runner.

```bash
npm install -g aws-cdk
cd infra
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk bootstrap                      # once per account/region
cdk deploy                         # add -c model_id=... to override the model
```

The stack (`infra/stack.py`) creates:

| Resource | Purpose |
|---|---|
| `opsrelay_coordinator` AgentCore Runtime (HTTP) | Entry point; runs the coordinator agent |
| `opsrelay_{triage,diagnostics,remediation,verification,communications}` Runtimes (A2A) | Specialist agents, each with its own agent card |
| DynamoDB table (on-demand, PITR) | Shared state: incidents, approvals, audit log, services |
| One IAM role per runtime | Bedrock model invoke, table read/write, logs and X-Ray. The coordinator may also invoke the five specialists. |

Operate the deployed platform with the same CLI:

```bash
export OPSRELAY_COORDINATOR_ARN=$(aws cloudformation describe-stacks --stack-name OpsRelay \
  --query "Stacks[0].Outputs[?OutputKey=='CoordinatorRuntimeArn'].OutputValue" --output text)
opsrelay simulate bad-deploy
opsrelay approvals
opsrelay approve apr-xxxxxxxxxx --by you@company.com
```

Or call it directly with the AWS SDK: `bedrock-agentcore` → `InvokeAgentRuntime` with a JSON payload (see `opsrelay/runtime/coordinator.py` for every action). Add `"async": true` to return immediately and poll with `get_incident`.

**Model id.** Any Bedrock model that supports tool use through the Converse API works. The
default is Amazon Nova 2 Lite (`global.amazon.nova-2-lite-v1:0`): Amazon's own models need no
AWS Marketplace subscription, so they work on accounts without a card set up for Marketplace.
Claude (for example `global.anthropic.claude-opus-5`) works too, but needs Anthropic's one-time
use case form and a Marketplace subscription with a valid payment method. Ids vary by account and region; list what your account has with:

```bash
aws bedrock list-inference-profiles --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'nova') || contains(inferenceProfileId, 'claude')].inferenceProfileId"
```

Then deploy with `cdk deploy -c model_id=<id>`. Amazon Nova Pro caps output at 10,000 tokens, so
with it also set `OPSRELAY_MAX_TOKENS=10000`.

## Configuration

Environment variables, prefixed `OPSRELAY_` (see `opsrelay/config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `MODEL_PROVIDER` | `offline` | `bedrock` (a model on Bedrock) or `offline` (scripted) |
| `BEDROCK_MODEL_ID` | `global.amazon.nova-2-lite-v1:0` | Bedrock model or inference-profile id |
| `MAX_TOKENS` | `16000` | Maximum output tokens per model call; lower it for models with a smaller limit |
| `STORE` | `sqlite` | `sqlite` (local) or `dynamodb` |
| `DYNAMODB_TABLE` | `opsrelay` | Table name when `STORE=dynamodb` |
| `SPECIALIST_TRANSPORT` | `local` | `local` (in-process) or `a2a` (remote agents) |
| `TRIAGE_ENDPOINT` etc. | | AgentCore runtime ARN or `http(s)://` A2A URL, per specialist |
| `POLICY_FILE` | built-in | Your own remediation policy (start from a copy of `opsrelay/policies.yaml`) |
| `AGENT_TIMEOUT_SECONDS` | `300` | Timeout for one call to a specialist |
| `AGENT_MAX_ATTEMPTS` | `3` | Attempts per call before the request is dead-lettered |
| `RETRY_BACKOFF_SECONDS` | `1.0` | First retry delay; doubles on each retry |
| `BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive failures that open an agent's circuit |
| `BREAKER_RECOVERY_SECONDS` | `30` | How long an open circuit fails fast before a trial call |
| `ROLE` | `coordinator` | Which agent a container serves |
| `ENVIRONMENT` | `simulated` | `simulated` (scenarios) or `aws` (CloudWatch metrics and logs, ECS actions; see below) |
| `SERVICE_CATALOG` | | YAML service catalog for `ENVIRONMENT=aws` (see `deploy/catalog.example.yaml`) |
| `AUTH_MODE` | `none` | `none`, `dev` or `oidc` (see Governance) |
| `DEV_USERS` | | Dev users file (`opsrelay users add`), or a Secrets Manager ARN holding it |
| `OIDC_ISSUER`, `OIDC_AUDIENCE` | | Your identity provider, e.g. a Cognito user pool and its app client id |
| `A2A_TOKEN` | | Shared token for specialist A2A servers outside AgentCore (may be a Secrets Manager ARN) |
| `EXECUTION_LEASE_SECONDS`, `JOB_LEASE_SECONDS` | `300`, `900` | How long a stopped executor or worker holds its lease before another takes over |
| `INTAKE_QUEUE_URL` | | SQS queue to read alerts from (EventBridge, SNS) |
| `WEBHOOK_TOKEN` | | Enables `POST /alerts` and is the bearer token it requires |
| `CORRELATION_WINDOW_MINUTES` | `30` | New alerts on a service with an open incident this recent join it |
| `JOB_QUEUE_URL` | | SQS queue for jobs (AWS): a Lambda runs them via `run_job`; no worker thread |
| `EMBEDDINGS` | `auto` | `bedrock` (Amazon Titan Text Embeddings v2), `lexical` (offline), or `auto`: Titan when the agents run on Bedrock |
| `RUNBOOK_DIR` | | A folder of your own Markdown runbooks (same id overrides a built-in) |
| `PUBLIC_URL` | | The dashboard's address, linked from Slack, Teams, PagerDuty and Jira |
| `ORIGIN_SECRET` | | Behind API Gateway or CloudFront: the `x-opsrelay-origin` value requests must carry |
| `SLACK_BOT_TOKEN`, `SLACK_CHANNEL`, `SLACK_USERS` | | Slack messages and Approve/Reject buttons (see Integrations) |
| `SLACK_APP_TOKEN` or `SLACK_SIGNING_SECRET` | | Button clicks over Socket Mode, or over HTTPS with signed requests |
| `TEAMS_WEBHOOK_URL` | | Microsoft Teams Workflows or incoming-webhook URL |
| `PAGERDUTY_ROUTING_KEY` | | PagerDuty Events API v2 integration key |
| `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT`, `JIRA_ISSUE_TYPE` | | Jira Cloud tickets |

## Governance and safety

| Layer | Where | What it guarantees |
|---|---|---|
| **State machine** | `lifecycle.py` | Only the moves in `ALLOWED_TRANSITIONS`. Status changes are compare-and-set writes; `update_incident` refuses to touch status. |
| **Atomic state + audit** | `store/` | Every write is one `Store.commit` (a SQLite transaction, a DynamoDB `TransactWriteItems`): a status change, its approvals and records, and the audit events describing it land together or not at all. The log can't miss a change or record one that didn't happen. |
| **Agent contracts** | `contracts.py` | Each agent acts only in its states, gets only its tools, makes only its transitions, and must leave its typed result. Breaches are refused and logged as `contract.violation`. |
| **Typed interfaces** | `schemas.py` | Agent output is validated by pydantic (`TriageResult`, `DiagnosisResult`, `RemediationProposal`, `VerificationResult`, `StatusUpdate`, `Postmortem`) before it reaches the workflow. Invalid output goes back to the agent with the errors. |
| **Policy engine** | `policy.py`, `policies.yaml` | ALLOW, APPROVAL_REQUIRED or DENY per proposal, with reasons: action allow-list, risk (raised on tier-1 services, never lowered below the agent's estimate), parameter bounds, confidence thresholds, a proposal limit. |
| **Approval engine** | `approvals.py` | One decision per approval (conditional write). A rejection escalates. |
| **Execution engine** | `executor.py` | The only code that changes infrastructure. Each action runs once per idempotency key, under a lease. If the executor dies mid-action, the next one takes the lease over and **reconciles** with the environment (applied / not applied / unknown) instead of guessing; unknown fails safe to a person. |
| **Separation of duties** | contracts | Remediation proposes; verification judges the outcome; only communications resolves; only the platform starts execution. |
| **Resilience** | `resilience.py`, `deadletter.py` | Timeouts, retries with exponential backoff, a circuit breaker per agent, a dead-letter queue, and escalation to a person. A retry is skipped if the lost attempt already did its job. |
| **Durable work** | `jobs.py` | Background work is a job in the shared store, claimed under a lease that a heartbeat renews. If a process dies, another worker takes the job over; a job that keeps failing is dead-lettered. Interrupted remediations are recovered automatically. |
| **Authentication** | `auth.py` | `OPSRELAY_AUTH_MODE=dev` (named users, hashed bearer tokens) or `oidc` (JWTs from Cognito or any OIDC provider, verified against its JWKS). With it on, a decision's approver is the signed-in person, never a typed name. |
| **Roles** | `rbac.py` | viewer, operator, sre, incident_commander, admin, auditor. Approving needs a role matching the risk and severity (SEV1 or critical: incident commander). Admins change policy but can't approve actions. |
| **Audited policy changes** | `policy_admin.py` | Policy versions are proposed, reviewed by a *second* admin, and activated; each step is on the `policy` audit chain. |
| **Tamper-evident audit** | `audit.py`, stores | Every event stores the previous event's hash; `opsrelay audit <incident>` finds the first altered or missing event. Events record the actor type, input and output hashes, and the agent, agent version, model and prompt version. |
| **Runbook citations** | `policy.py`, `runbooks.py` | A proposal cites the runbook it follows. No citation, an unknown runbook, or an action the runbook doesn't recommend: a person must approve, and the policy says why ("RB-002 recommends restart_service, rollback_deployment; not scale_service"). |
| **Untrusted input** | prompts, evals | Alert and log text reach the agents as data; the prompts say so, and the `prompt-injection` eval case checks it. |
| **Agent-to-agent auth** | `a2a_auth.py`, `remote.py` | On AgentCore, SigV4 and IAM. Elsewhere, specialists require a shared bearer token (`OPSRELAY_A2A_TOKEN`; `opsrelay up` generates one per run). |
| **Least-privilege IAM** | `infra/stack.py` | Bedrock access only to the configured model. DynamoDB writes limited per runtime by key prefix, mirroring the agent contracts. Secrets from Secrets Manager (`secrets.py`). |

`opsrelay contracts` prints the lifecycle, every contract and who may make each move;
`opsrelay policy test ACTION SERVICE` shows the decision for a proposal.

## Security

Out of the box OpsRelay runs with authentication off, for local development: the dashboard shows
the approver's name as **unverified**. To require sign-in:

```powershell
opsrelay users add "Jane" --roles sre,incident_commander --file users.yaml   # prints Jane's token once
opsrelay users add "Ada" --roles admin --file users.yaml
$env:OPSRELAY_AUTH_MODE="dev"; $env:OPSRELAY_DEV_USERS="users.yaml"
opsrelay up                                    # the dashboard now asks for a token
opsrelay --url http://127.0.0.1:8080 --token <token> whoami
```

For production, use `OPSRELAY_AUTH_MODE=oidc` with your identity provider (for Amazon Cognito:
`OPSRELAY_OIDC_ISSUER=https://cognito-idp.<region>.amazonaws.com/<pool id>`,
`OPSRELAY_OIDC_AUDIENCE=<app client id>`, and user groups named after the roles). Keep the dev users
file or tokens in Secrets Manager by passing its ARN instead of a path.

Policy changes go through two admins:

```powershell
opsrelay policy propose my-policy.yaml --note "allow restarts without approval"   # Ada
opsrelay policy approve v1                                                        # Bob (not Ada)
opsrelay policy activate v1
opsrelay policy history; opsrelay audit policy
```

## Alerts in: event-driven intake

Incidents open from real alerts, not only from the API and the demo scenarios:

```
CloudWatch alarm --> EventBridge rule --> SQS (+ DLQ) --> OpsRelay intake --> dedup / correlate --> incident
Alertmanager -----> webhook POST /alerts, or SNS --> SQS ------^
```

- **Parsers** for EventBridge "CloudWatch Alarm State Change" events, classic alarm notifications
  via SNS, and Alertmanager v4 webhooks (`opsrelay/intake/alerts.py`).
- **Deduplication:** a repeat of an open incident's alert (same alarm ARN or Alertmanager
  fingerprint) is counted on that incident. **Correlation:** a new alert on a service with an open
  incident, within `OPSRELAY_CORRELATION_WINDOW_MINUTES`, is attached to it, and so is an alert on
  a direct dependency either way (from the service catalog): a downstream symptom, or a possible
  root cause that diagnostics is told to check. **Resolved** alerts are
  noted; verification still decides. Race-free: the fingerprint is claimed before the incident is
  created.
- **Where alerts come in:** `OPSRELAY_INTAKE_QUEUE_URL` (an SQS consumer, for EC2 or `opsrelay up`),
  `POST /alerts` with `Authorization: Bearer $OPSRELAY_WEBHOOK_TOKEN` (point Alertmanager's
  webhook receiver at it), the `ingest_alert` API action, or `opsrelay alert alarm.json`.
- **On AgentCore,** the CDK stack wires it for you: EventBridge rule and SNS topic into an SQS
  queue with a dead-letter queue, consumed by an intake Lambda. Coordination jobs go through a
  second SQS queue and a worker Lambda (`OPSRELAY_JOB_QUEUE_URL`), and a scheduled Lambda finishes
  interrupted work every 5 minutes, so nothing depends on a long-running process.

## Observability

- `opsrelay metrics`, the `get_metrics` action and the console's **Operations** panel: time to
  detect, diagnose, acknowledge (MTTA), remediate, verify and resolve (MTTR) as median and p90;
  incident outcomes; per-agent calls, latency and failures; policy decisions; alert deduplication.
  Computed from the audit trail, so no metrics backend is needed.
- OpenTelemetry metrics (`opsrelay_incidents_total`, `opsrelay_stage_duration_seconds`,
  `opsrelay_agent_latency_seconds`, `opsrelay_policy_decisions_total`, ...) and trace spans for
  coordination, delegation, execution and intake. Set `OTEL_EXPORTER_OTLP_ENDPOINT` and
  `pip install "opsrelay[otel]"` to export them to any OpenTelemetry collector.

## Knowledge: runbooks and incident memory

**Runbooks** are Markdown files with YAML front matter (`opsrelay/runbooks/`, plus your own in
`OPSRELAY_RUNBOOK_DIR`):

```markdown
---
id: RB-002
title: Memory exhaustion and OOMKilled pods
categories: [memory-leak]
services: []                                   # empty: any service
actions: [restart_service, rollback_deployment]  # what it recommends; [] = diagnose, then escalate
---
1. Confirm memory is near the limit and pods are being OOMKilled. ...
```

The diagnostics and remediation agents search them with `search_runbooks` (Amazon Titan Text
Embeddings v2 on Bedrock, a local lexical embedder offline, plus a boost for the matching category
and service), and a proposal cites the runbook's id. The dashboard shows the runbook on the
approval card.

**Incident memory:** when an incident closes, OpsRelay records its symptoms, service, root cause,
evidence, the action taken and its outcome, any proposals people rejected and why, how long it
took, and the postmortem's lessons. `find_similar_incidents` gives the agents the closest past
incidents, so a correction a person made once isn't needed twice; the dashboard shows them on
each incident.

```powershell
opsrelay runbooks "pods OOMKilled after deploy" --category memory-leak
opsrelay similar inc-xxxxxxxxxx          # or: opsrelay similar "checkout 5xx after release"
opsrelay postmortem inc-xxxxxxxxxx -o postmortem.md   # a draft from the record if it was escalated
opsrelay policy test scale_service auth-service --replicas 4 --runbook RB-002
```

## Evaluation and replay

```powershell
opsrelay eval                                        # every case in opsrelay/eval_cases.yaml, offline agents by default
opsrelay eval --model global.amazon.nova-2-lite-v1:0 --runs 3 --save
opsrelay eval --fail-under 0.9                       # a CI gate: exit 1 on low accuracy or any unsafe proposal
opsrelay replay inc-xxxxxxxxxx --model global.amazon.nova-pro-v1:0
```

Each eval case raises an alert in a fresh sandbox (its own store and simulated environment; your
incidents are never touched) and scores triage, diagnosis, action and escalation accuracy, unsafe
proposals, runbook citations, contract violations and latency. The built-in cases include a vague
alert, a prompt injection in the alert text and a false alarm. `replay` reruns a past incident the
same way, deciding as the person did then, and lists what the agents now do differently.

## Integrations: Slack, Teams, PagerDuty, Jira

| When | Slack | Teams | PagerDuty | Jira |
|---|---|---|---|---|
| A proposal needs a person | message with **Approve** / **Reject** | card linking to the dashboard | | |
| SEV1, or escalated | message (escalated) | card (escalated) | incident (dedup key = incident id) | ticket |
| Resolved | message | card | resolved, if paged | comment with the postmortem, if ticketed |

Configure any of them with the `OPSRELAY_SLACK_*`, `TEAMS_*`, `PAGERDUTY_*` and `JIRA_*` settings
(tokens may be Secrets Manager ARNs). Notifications go through an outbox: each is created once and
delivered by a durable job with retries, so a restart neither loses nor repeats one, and a failing
channel never holds up the incident.

**Approving from Slack:** create a Slack app with a bot token (`chat:write`) and choose how button
clicks reach OpsRelay:

- **Socket Mode** (simplest; no public URL): turn on Socket Mode and Interactivity in the app, and
  set `OPSRELAY_SLACK_APP_TOKEN` to its App-Level Token (`xapp-`, scope `connections:write`).
  OpsRelay opens the connection to Slack itself (`pip install "opsrelay[slack]"`).
- **HTTPS**: turn on Interactivity with the request URL
  `https://<your OpsRelay>/integrations/slack/actions` and set `OPSRELAY_SLACK_SIGNING_SECRET`
  (Basic Information → App Credentials → Signing Secret, 32 hex characters; not the `xapp-` token).

Secrets can be Secrets Manager references, including one key of a key/value secret
(`secretsmanager:opsrelay/slack#bot-token`). The `integrations` API action shows what's on and
whether Socket Mode is connected. Map Slack users to OpsRelay users and roles in
`OPSRELAY_SLACK_USERS` (or add `slack_id` to entries in the dev users file):

```yaml
users:
  - {slack_id: U0123ABCD, name: jane@example.com, roles: [sre, incident_commander]}
```

Every click is checked against Slack's request signature and authorized by role exactly like the
dashboard; unmapped users can't decide. The endpoint must be reachable by Slack (EC2 or `opsrelay
up` behind HTTPS); on AgentCore, messages, pages and tickets work and Teams/Slack link to the
dashboard.

## Connecting real systems

`OPSRELAY_ENVIRONMENT=aws` replaces the simulation with real services: a YAML service catalog
(tiers, owners, dependencies, log groups, ECS services, health thresholds; see
`deploy/catalog.example.yaml`), metrics from CloudWatch, logs from CloudWatch Logs, and
rollbacks, restarts and scaling on Amazon ECS. Before changing anything, OpsRelay tags the ECS
service with the intended change, so after a crash it can tell whether the change happened.
Services without an ECS entry are observed but never changed.

## Extending

- **Change the policy.** Copy `opsrelay/policies.yaml`, edit it, and point `OPSRELAY_POLICY_FILE` at it.
- **Connect real systems.** Implement `Environment` in `opsrelay/environment.py` (for example CloudWatch metrics and logs, your CMDB, your deploy tool), and return it from `get_environment()`. The simulated environment shows the contract.
- **Add a specialist.** Add its tools in `agents/tools.py`, a prompt in `agents/prompts.py`, a contract in `contracts.py` and its role to `SPECIALISTS`. It gets its own A2A runtime from the CDK stack, and the coordinator gets an `ask_<role>` delegation tool.
- **Call external A2A agents.** Any A2A-compliant agent can be a specialist: point `OPSRELAY_<ROLE>_ENDPOINT` at its URL.

## Project layout

```
opsrelay/
  agents/        Strands agents: prompts, tools, factory (audit hook, contract-enforcing A2A delegation)
  runtime/       AgentCore entrypoints: coordinator (HTTP) and specialists (A2A)
  store/         SQLite and DynamoDB stores (same contract: compare-and-set status, hash-chained events)
  lifecycle.py   incident state machine and the only way to change status
  contracts.py   per-agent contracts: states, tools, transitions, results, postconditions
  schemas.py     typed agent interfaces (pydantic)
  policy.py      policy engine; policies.yaml holds the rules
  approvals.py   approval engine
  executor.py    execution engine (idempotent)
  resilience.py  retries, timeouts, circuit breakers; deadletter.py: the dead-letter workflow
  jobs.py        durable job queue and worker (leases, heartbeats, recovery)
  auth.py        authentication (none, dev tokens, OIDC); rbac.py: roles and permissions
  policy_admin.py versioned, reviewed, audited policy changes
  a2a_auth.py    bearer-token auth for specialist A2A servers; secrets.py: Secrets Manager
  connectors/    AWS environment: service catalog, CloudWatch, CloudWatch Logs, ECS
  intake/        alert parsers (CloudWatch, Alertmanager), dedup/correlation router, SQS consumer
  telemetry.py   OpenTelemetry metrics and traces; ops_metrics.py: MTTA, MTTR, stage times
  audit.py       audit hash-chain verification
  knowledge.py   embeddings (Amazon Titan v2, or lexical offline); runbooks.py + runbooks/: runbook RAG
  memory.py      incident memory and similar-incident search; postmortem.py: postmortem export and drafts
  evals.py       evaluation suite (eval_cases.yaml) and incident replay
  integrations.py Slack, Teams, PagerDuty and Jira through a notification outbox
  environment.py connector interface + simulated IT environment and scenarios
  offline.py     scripted Strands model provider for offline runs
  remote.py      A2A client with SigV4 for AgentCore runtimes
  service.py     incident operations used by the runtime and CLI
  cli.py         `opsrelay` command
infra/           AWS CDK app (AgentCore runtimes, DynamoDB, IAM, EventBridge, SQS, Lambdas)
tests/           lifecycle, contracts, policy, approvals, jobs, auth, connectors, workflow, A2A, runtime, stores,
                 agents, infra, knowledge, memory, evals, integrations
docs/            USER_GUIDE.md: the workflow in screenshots; WALKTHROUGH.md: hands-on tour;
                 ARCHITECTURE.md: design decisions and next steps
deploy/          EC2 user data; an example service catalog; a Slack app manifest
```

## Development

```bash
pip install -e ".[dev]" aws-cdk-lib constructs
pytest            # 199 tests, fully offline
ruff check . && ruff format --check .
```

## License

MIT
