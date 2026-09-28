# Architecture and design decisions

## Components

| Component | Where it runs | Built with |
|---|---|---|
| Coordinator agent | AgentCore Runtime, HTTP protocol (`POST /invocations`, port 8080) | Strands `Agent` + `BedrockAgentCoreApp` |
| Triage, diagnostics, remediation, verification, communications agents | One AgentCore Runtime each, A2A protocol (JSON-RPC at `/`, port 9000) | Strands `Agent` + `StrandsA2AExecutor` + `build_a2a_app` |
| Governance layer | Inside every runtime (platform code, not tools the model can bypass) | `lifecycle.py`, `contracts.py`, `schemas.py`, `policy.py`, `approvals.py`, `executor.py` |
| Shared state | DynamoDB single table | `opsrelay/store/dynamodb.py` |
| Model | Amazon Nova (default) or Claude on Amazon Bedrock | Strands `BedrockModel` |

All six runtimes use one container image. `OPSRELAY_ROLE` selects the agent.

## From model output to infrastructure

An agent's output never drives the workflow directly:

```
LLM output -> pydantic validation -> agent contract -> lifecycle -> policy engine -> approval -> executor -> verification
```

1. **Typed results** (`schemas.py`). Each specialist submits its result through one tool whose
   arguments are validated against a pydantic model. Invalid output returns the validation errors
   to the agent, which corrects itself; nothing invalid is stored.
2. **Contracts** (`contracts.py`). Each agent's contract lists the incident states it acts in, its
   tools (least privilege), the lifecycle moves it may make, its result models, and a postcondition.
   The coordinator's delegation tool checks the state before dispatching and the postcondition
   after the agent returns; tools check the state again on the specialist's side. Breaches are
   refused and logged as `contract.violation`.
3. **Lifecycle** (`lifecycle.py`). `transition()` is the only way to change an incident's status.
   It checks `ALLOWED_TRANSITIONS`, checks that the actor's contract owns the move, and writes it
   with a compare-and-set on the current status (a SQLite `BEGIN IMMEDIATE` transaction, a
   DynamoDB `ConditionExpression`). Stores refuse status changes made any other way.
4. **Policy engine** (`policy.py`, `policies.yaml`). A pure function of the proposal and facts
   (service tier and limits, deployed versions, diagnosis confidence, proposals so far). It returns
   ALLOW, APPROVAL_REQUIRED or DENY with reasons and the policy version, so every decision can be
   explained, tested (`opsrelay policy test`) and replayed.
5. **Approval engine** (`approvals.py`). Denied proposals are recorded and explained to the agent.
   Otherwise the incident moves to `awaiting_approval`; ALLOW decisions are approved by the policy
   at once, the rest wait for a person. Only one decision wins (conditional write). A rejection
   escalates.
6. **Execution engine** (`executor.py`). The only code that calls `Environment.execute`. It claims
   an idempotency key (`incident:action:service:params-hash`) before running, so a retry or a
   duplicate decision returns the first result instead of acting twice.
7. **Verification.** A separate agent checks recovery; the platform cross-checks its claim against
   live metrics before accepting it. No recovery moves the incident to `failed`.

## Human approval: durable gates, not in-memory interrupts

Strands supports human-in-the-loop interrupts that pause an agent mid-run. OpsRelay doesn't use
them for approvals, because an approval can take hours, and a paused agent's state lives in a
runtime session that times out (15 minutes idle by default). Instead a proposal is written as a
pending approval and the agent's run ends; the coordinator is invoked again when a person decides,
and continues from the incident's state.

## Agent-to-agent communication

The coordinator reaches each specialist through an `ask_<role>` tool (`agents/factory.py`). The
tool enforces the contract and calls an *invoker*:

- **In-process** (`local_invoker`): for development and tests.
- **Over A2A** (`remote.a2a_invoker`): wraps Strands' `A2AAgent` client. For an AgentCore runtime
  ARN, requests go to the runtime's invocation URL and are signed with SigV4 using the
  coordinator's IAM role.

Each call has a timeout and is retried with exponential backoff behind a per-agent circuit breaker
(`resilience.py`). Before a retry, the postcondition is checked: if the lost attempt already did
its job, nothing is repeated. When every attempt fails, the request goes to the dead-letter queue
(`deadletter.py`) and the incident to a person through legal transitions only.

Specialists are stateless between requests; everything they learn goes into the shared store. Any
A2A-compliant agent can replace a specialist by pointing `OPSRELAY_<ROLE>_ENDPOINT` at it, and the
contract still applies, because it is enforced by the coordinator and the shared tools.

## Audit

Every event is appended to its incident's hash chain: it stores `prev_hash` (the previous event's
hash) and `hash` (SHA-256 of the event including `prev_hash`). On DynamoDB each event is written in
one transaction with the incident's `CHAIN` head item, conditioned on the previous head, so
concurrent writers can't fork the chain. `audit.verify_chain` finds the first altered, removed or
reordered event.

Events record the actor and its type (agent, human, platform, source), input and output hashes,
and for agents the agent version, model and prompt version, so "why did the system decide this?"
can be answered later. Event kinds include `incident.created`, `incident.triaged`,
`diagnosis.completed`, `remediation.proposed`, `policy.evaluated`, `approval.requested`,
`approval.approved`, `tool.invoked`, `tool.completed`, `verification.completed`,
`incident.resolved`, `status.changed`, `contract.violation` and `agent.unavailable`.

## Offline mode

`offline.ScriptedModel` implements the Strands `Model` interface and drives the same agents with
deterministic playbooks that follow the same contracts. The whole system runs in CI with no AWS
account or model spend: tools, hooks, contracts, policy, A2A servers, the runtime contract, and a
Docker Compose topology. When a real model behaves differently, the offline tests show whether the
platform or the model changed.

## What to build next

- **Intelligence:** runbook retrieval, incident memory (similar past incidents, postmortems and
  rejection notes), an evaluation suite per agent, and incident replay against new model versions.
- **Enterprise:** authentication and roles (viewer, operator, incident commander, SRE, admin,
  auditor), Secrets Manager, EventBridge/SQS intake, Slack or Teams approvals, OpenTelemetry
  tracing and CloudWatch metrics.
- **Real connectors:** implement `Environment` for CloudWatch, your CMDB and your deploy tool.
