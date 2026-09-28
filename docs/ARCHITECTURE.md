# Architecture and design decisions

## Components

| Component | Where it runs | Built with |
|---|---|---|
| Coordinator agent | AgentCore Runtime, HTTP protocol (`POST /invocations`, port 8080) | Strands `Agent` + `BedrockAgentCoreApp` |
| Triage, diagnostics, remediation, communications agents | One AgentCore Runtime each, A2A protocol (JSON-RPC at `/`, port 9000) | Strands `Agent` + `StrandsA2AExecutor` + `build_a2a_app` |
| Shared state | DynamoDB single table | `opsrelay/store/dynamodb.py` |
| Model | Claude on Amazon Bedrock | Strands `BedrockModel` |

All five runtimes use one container image. `OPSRELAY_ROLE` selects the agent.

## Agent-to-agent communication

The coordinator reaches each specialist through an `ask_<role>` tool (`agents/factory.py`). The tool calls an *invoker*:

- **In-process** (`local_invoker`): for development and tests.
- **Over A2A** (`remote.a2a_invoker`): wraps Strands' `A2AAgent` client.

  For an AgentCore runtime ARN, requests go to the runtime's invocation URL and are signed with SigV4 using the coordinator's IAM role. Each request carries a fresh runtime session id.

A runtime doesn't know its own ARN when it's created, so the A2A client always sends to the endpoint it resolved, whatever URL the card advertises.

Specialists are stateless between requests. Everything they learn goes into the shared store, and a new agent is built for every A2A context. Any request can land on any runtime session, and a restart loses nothing.

Any A2A-compliant agent, including agents on other platforms, can replace a specialist by pointing `OPSRELAY_<ROLE>_ENDPOINT` at it.

## Human approval: durable gates, not in-memory interrupts

Strands supports human-in-the-loop interrupts that pause an agent mid-run. OpsRelay doesn't use them for approvals, because an approval can take hours, and a paused agent's state lives in a runtime session that times out (15 minutes idle by default).

Instead:

1. `propose_action` writes a **pending approval** to DynamoDB and returns immediately. The agent's run ends.
2. A person decides later through the coordinator's `decide_approval` action. `approvals.decide` changes the approval state with a conditional write, so only one decision wins. On approval it executes the action through the `Environment` connector.
3. The coordinator is invoked again with the outcome and carries on: verify, communicate, resolve or escalate.

Execution lives in platform code the model can't reach. No agent has an "execute" tool, and a test (`tests/test_agents.py`) enforces that.

## Audit

- `AuditHook` (a Strands `HookProvider` on `AfterToolCallEvent`) logs every tool call with its input and a result preview.
- The delegation tools log each A2A request and response.
- Approval code logs each decision and execution, with the person's identity.

Events are append-only: `INC#<id>` / `EVT#<ts>#<uuid>` items in DynamoDB.

## Offline mode

`offline.ScriptedModel` implements the Strands `Model` interface and drives the same agents with deterministic playbooks. The whole system can then be exercised in CI with no AWS account or model spend: tools, hooks, A2A servers, the runtime contract, and a Docker Compose topology. It is also a regression suite for the orchestration logic: when a real model behaves differently, the offline tests show whether the platform or the model changed.

## What to build next

- **Real connectors.** Implement `Environment` for CloudWatch (metrics and logs), your CMDB, and your deploy tool (for example CodeDeploy or Argo CD).
- **Intake.** Alert webhooks (Alertmanager, CloudWatch alarms through EventBridge and Lambda), and a Slack or Teams approval flow that calls `decide_approval`.
- **Approver identity.** An AgentCore JWT authorizer (Cognito or your IdP) on the coordinator, so approver identity comes from a verified token, not the payload.
- **Guardrails and observability.** Bedrock Guardrails on the model, and AgentCore Observability dashboards.
