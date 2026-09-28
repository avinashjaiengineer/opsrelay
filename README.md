# OpsRelay

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

![OpsRelay dashboard: an alert fires, the agents investigate, a person approves, the incident is resolved](docs/images/workflow.gif)

See the [user guide](docs/USER_GUIDE.md) for the whole workflow in screenshots, why it's useful,
and ideas for improving it.

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
4. **Propose.** Remediation submits a `RemediationProposal`: action, parameters, its own risk estimate, rollback plan.
5. **Policy** (`awaiting_approval`). The policy engine (`opsrelay/policies.yaml`) returns ALLOW, APPROVAL_REQUIRED or DENY, with reasons. Diagnosis confidence below 0.70 is denied; below 0.90 always needs a person.
6. **Human gate.** A person approves or rejects. A rejection **escalates** the incident.
7. **Execute** (`remediating`). The execution engine runs the action once, keyed by an idempotency key.
8. **Verify** (`verifying`). The verification agent, not the one that proposed the fix, checks recovery; the platform cross-checks its claim against live metrics. No recovery means `failed`, then `escalated`.
9. **Close** (`resolved`). Communications posts internal and customer updates, then submits a typed `Postmortem`.

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

## Governance and safety

| Layer | Where | What it guarantees |
|---|---|---|
| **State machine** | `lifecycle.py` | Only the moves in `ALLOWED_TRANSITIONS`. Status changes are compare-and-set writes (SQLite transaction, DynamoDB `ConditionExpression`); `update_incident` refuses to touch status. |
| **Agent contracts** | `contracts.py` | Each agent acts only in its states, gets only its tools, makes only its transitions, and must leave its typed result. Breaches are refused and logged as `contract.violation`. |
| **Typed interfaces** | `schemas.py` | Agent output is validated by pydantic (`TriageResult`, `DiagnosisResult`, `RemediationProposal`, `VerificationResult`, `StatusUpdate`, `Postmortem`) before it reaches the workflow. Invalid output goes back to the agent with the errors. |
| **Policy engine** | `policy.py`, `policies.yaml` | ALLOW, APPROVAL_REQUIRED or DENY per proposal, with reasons: action allow-list, risk (raised on tier-1 services, never lowered below the agent's estimate), parameter bounds, confidence thresholds, a proposal limit. |
| **Approval engine** | `approvals.py` | One decision per approval (conditional write). A rejection escalates. |
| **Execution engine** | `executor.py` | The only code that changes infrastructure. Each action runs once per idempotency key, so retries and duplicate events can't repeat it. |
| **Separation of duties** | contracts | Remediation proposes; verification judges the outcome; only communications resolves; only the platform starts execution. |
| **Resilience** | `resilience.py`, `deadletter.py` | Timeouts, retries with exponential backoff, a circuit breaker per agent, a dead-letter queue, and escalation to a person. A retry is skipped if the lost attempt already did its job. |
| **Tamper-evident audit** | `audit.py`, stores | Every event stores the previous event's hash; `opsrelay audit <incident>` finds the first altered or missing event. Events record the actor type, input and output hashes, and the agent, agent version, model and prompt version. |
| **Untrusted input** | prompts | Alert and log text reach the agents as data; the prompts say so. |
| **IAM** | `infra/stack.py` | SigV4 between agents on AWS; each runtime role has only the permissions it needs. |

`opsrelay contracts` prints the lifecycle, every contract and who may make each move;
`opsrelay policy test ACTION SERVICE` shows the decision for a proposal.

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
  audit.py       audit hash-chain verification
  environment.py connector interface + simulated IT environment and scenarios
  offline.py     scripted Strands model provider for offline runs
  remote.py      A2A client with SigV4 for AgentCore runtimes
  service.py     incident operations used by the runtime and CLI
  cli.py         `opsrelay` command
infra/           AWS CDK app (AgentCore runtimes, DynamoDB, IAM)
tests/           lifecycle, contracts, policy, approvals, workflow, A2A, runtime, stores, agents, infra
docs/            USER_GUIDE.md: the workflow in screenshots; WALKTHROUGH.md: hands-on tour;
                 ARCHITECTURE.md: design decisions and next steps
deploy/ec2/      user data to run OpsRelay on an EC2 instance
```

## Development

```bash
pip install -e ".[dev]" aws-cdk-lib constructs
pytest            # 97 tests, fully offline
ruff check . && ruff format --check .
```

## License

MIT
