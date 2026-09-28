# OpsRelay

**An agent-to-agent platform for enterprise IT incident resolution, built on [Strands Agents](https://strandsagents.com) and hosted on [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/).**

When an alert fires, a coordinator agent hands the incident to specialist agents over the
**A2A (Agent2Agent) protocol**:

- the **triage** agent sets severity,
- the **diagnostics** agent finds the root cause,
- the **remediation** agent proposes a runbook fix,
- the **communications** agent updates stakeholders and writes the postmortem.

Nothing touches your infrastructure until a person approves it.

```
 alert / API / CLI
        │
        ▼
┌──────────────────┐   A2A (JSON-RPC, SigV4)   ┌──────────────────┐
│   coordinator    │──────────────────────────▶│  triage          │
│ AgentCore Runtime│──────────────────────────▶│  diagnostics     │  4 AgentCore Runtimes
│  (HTTP protocol) │──────────────────────────▶│  remediation     │  (A2A protocol)
│                  │──────────────────────────▶│  communications  │
└────────┬─────────┘                           └────────┬─────────┘
         │                                              │
         ▼                                              ▼
┌────────────────────────────────────────────────────────────────┐
│ DynamoDB: incidents · approvals · append-only audit log · CMDB │
└────────────────────────────────────────────────────────────────┘
         ▲
         │  approve / reject  (a person, via CLI or API)
```

## How an incident flows

1. **Alert in.** An incident is opened from an alert (`simulate`), the API (`open_incident`) or the CLI.
2. **Triage.** The coordinator delegates over A2A. Triage reads service health and the CMDB, picks the affected service and sets the severity.
3. **Diagnose.** Diagnostics checks metrics, logs and recent deployments and records a root cause with evidence.
4. **Propose.** Remediation finds the runbook and *proposes* an action (rollback, restart, scale, flush cache). The proposal becomes a **pending approval**. The risk is rated from the action and the service tier.
5. **Human gate.** A person approves or rejects it. Only then does the platform, not the agent, execute the action.
6. **Verify and close.** Remediation checks that the service recovered. Communications posts internal and customer updates, writes a blameless postmortem and resolves the incident. A rejection, a failed action or no recovery **escalates** the incident to the owning team.

Every delegation, tool call, human decision and executed action goes into an append-only audit log, with who did it.

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

This starts the four specialist agents as separate A2A servers (ports 9001-9004) and the
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
| `opsrelay_{triage,diagnostics,remediation,communications}` Runtimes (A2A) | Specialist agents, each with its own agent card |
| DynamoDB table (on-demand, PITR) | Shared state: incidents, approvals, audit log, services |
| One IAM role per runtime | Bedrock model invoke, table read/write, logs and X-Ray. The coordinator may also invoke the four specialists. |

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
| `AUTO_APPROVE_RISK` | `none` | Let actions at or below `low`/`medium` risk run without a person |
| `ROLE` | `coordinator` | Which agent a container serves |

## Safety model

- **Agents cannot act on infrastructure.** `propose_action` only records a pending approval. Execution happens in `opsrelay/approvals.py` after a person approves, and that path is enforced in code, not in a prompt.
- **One decision per approval.** Approval state changes use conditional writes (a SQLite `WHERE` clause, a DynamoDB `ConditionExpression`), so two people can't both decide the same approval.
- **Risk-rated actions.** Each action has a base risk, raised one level on tier-1 services. The auto-approve policy is off by default.
- **Guarded tools.** For example, `mark_mitigated` refuses while metrics are unhealthy, `resolve_incident` refuses unless the incident is mitigated, and scaling is bounded by `max_replicas`.
- **Untrusted input.** Alert text reaches the agents as data inside `<alert>` tags. The prompts tell the agents that log and alert content is data, not instructions.
- **Audit.** Every tool call, A2A request and response, and human decision is logged with its actor.
- **IAM.** SigV4 authenticates agent-to-agent calls on AWS. Each runtime role has only the permissions it needs.

## Extending

- **Connect real systems.** Implement `Environment` in `opsrelay/environment.py` (for example CloudWatch metrics and logs, your CMDB, your deploy tool), and return it from `get_environment()`. The simulated environment shows the contract.
- **Add a specialist.** Add its tools in `agents/tools.py`, a prompt in `agents/prompts.py` and its role to `SPECIALISTS`. It gets its own A2A runtime from the CDK stack, and the coordinator gets an `ask_<role>` delegation tool.
- **Call external A2A agents.** Any A2A-compliant agent can be a specialist: point `OPSRELAY_<ROLE>_ENDPOINT` at its URL.

## Project layout

```
opsrelay/
  agents/        Strands agents: prompts, tools, factory (+ audit hook, A2A delegation tools)
  runtime/       AgentCore entrypoints: coordinator (HTTP) and specialists (A2A)
  store/         SQLite and DynamoDB stores (same contract)
  approvals.py   human approval gate, risk policy, execution
  environment.py connector interface + simulated IT environment and scenarios
  offline.py     scripted Strands model provider for offline runs
  remote.py      A2A client with SigV4 for AgentCore runtimes
  service.py     incident operations used by the runtime and CLI
  cli.py         `opsrelay` command
infra/           AWS CDK app (AgentCore runtimes, DynamoDB, IAM)
tests/           workflow, A2A, runtime contract, stores (SQLite + moto DynamoDB), approvals, agents, infra
docs/            WALKTHROUGH.md: hands-on tour; ARCHITECTURE.md: design decisions and next steps
```

## Development

```bash
pip install -e ".[dev]" aws-cdk-lib constructs
pytest            # 50 tests, fully offline
ruff check . && ruff format --check .
```

## License

MIT
