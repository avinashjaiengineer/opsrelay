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

## Atomic state and audit

Every write goes through `Store.commit`, one transaction: a SQLite `BEGIN IMMEDIATE`, or a DynamoDB
`TransactWriteItems` of up to 100 items. A commit can change an incident (conditioned on its
status), create or move approvals (conditioned on their status), create or move records
(conditioned on their revision), and append audit events. The events are sealed onto their
incident's hash chain inside the same transaction; on DynamoDB each incident's `CHAIN` item holds
the head hash and the event count, the transaction is conditioned on it, and each event is keyed by
its position in the chain. So a status change and the events describing it exist together or not
at all, and concurrent writers can neither fork nor reorder a chain. If a DynamoDB transaction is
cancelled, the conditions are re-read: a real conflict returns "no change"; a lost race on a chain
head retries.

## Executions: leases and reconciliation

An execution is a record keyed by its idempotency key and held under a lease
(`execution_lease_seconds`). A finished execution replays its result. A live lease means another
executor is working on it: the incident stays `remediating` rather than failing. An expired lease
means the executor died mid-action: the next one takes the lease over (compare-and-set on the
revision) and asks the environment what happened, `Environment.reconcile`: *applied* (record
success, don't repeat), *not applied* (run it once), or *unknown* (fail safe: the incident fails and
goes to a person). Environments tag each change with its key: the simulation keeps applied keys on
the service; the ECS connector tags the service with the intended change *before* making it, then
compares the service's actual state with that target. `approvals.recover` finds interrupted
remediations; the worker runs it every `recovery_interval_seconds`.

## Durable work

The coordinator runtime no longer runs background work in bare threads. An async request enqueues a
job record; a worker claims it with a lease that a heartbeat renews while it runs. If the process
dies, the lease expires and any worker, after a restart or in another replica, takes the job over.
Re-running a coordination job is safe because the coordinator acts on the incident's state and
never repeats a step that succeeded. A job that fails `job_max_attempts` times is dead-lettered and
its incident handed to a person. (On AWS, the natural next step is an SQS queue in front of the
coordinator; the job model maps onto it directly.)

## Security

- **Authentication** (`auth.py`): `none` for development, `dev` bearer tokens (stored as SHA-256
  hashes), or `oidc` JWTs verified against the provider's JWKS. ASGI middleware authenticates every
  API call; the dashboard page and `/ping` stay public.
- **Authorization** (`rbac.py`): six roles; each API action names the roles allowed. Deciding a
  remediation needs a role that matches its risk and the incident's severity, and with
  authentication on the approver is the authenticated principal, stored with its role and
  `identity_verified` on the approval and its audit event.
- **Policy changes** (`policy_admin.py`): versions stored with author, reviewer and activation;
  the reviewer must differ from the author; each step is an event on the `policy` audit chain.
- **Agent-to-agent**: SigV4 and IAM on AgentCore; a shared bearer token elsewhere (`a2a_auth.py`).
- **IAM**: Bedrock scoped to the configured model; DynamoDB writes scoped per runtime with
  `dynamodb:LeadingKeys`, mirroring the agent contracts; secrets from Secrets Manager.

## Event-driven intake

Alerts from CloudWatch (EventBridge or SNS) and Alertmanager are normalized into one `Alert` with a
fingerprint, then routed (`intake/router.py`): duplicates of an open incident's alert are counted
on it, alerts on a service with a recent open incident are correlated with it, others open a new
incident whose coordination is queued as a job. The fingerprint is claimed (an `alert_key` record,
insert-if-absent or compare-and-set) with the incident id chosen *before* the incident is created,
so concurrent copies can't open two incidents. On AWS the stack puts SQS queues with dead-letter
queues at the boundary and Lambdas behind them: intake calls `ingest_alert`, a worker calls
`run_job`, and a schedule calls `recover`. Queue retries and DLQs sit on top of the platform's own
leases, so a message can be delivered twice without work being done twice.

## Observability

`telemetry.py` emits OpenTelemetry metrics and spans at the platform's decision points (lifecycle
moves, delegations, policy decisions, executions, intake), exported over OTLP when configured.
`ops_metrics.py` computes the operator's numbers (MTTA, MTTR, time per stage, agent health) from
the audit trail, so they're available, and auditable, without a metrics backend.

## What to build next

Done: atomic state + audit, execution leases and reconciliation, durable jobs (SQS-driven on AWS),
A2A authentication, CloudWatch/ECS connectors, authentication and roles, audited policy changes,
least-privilege IAM, Secrets Manager, event-driven intake with deduplication and correlation,
OpenTelemetry metrics and traces, MTTA/MTTR. Still open:

- **More sources and connectors:** PagerDuty, Datadog and Kubernetes events; EKS, your deploy tool,
  a CMDB, ticketing.
- **Richer correlation:** across dependent services (the CMDB graph), not only the same service.
- **Intelligence:** runbook retrieval, incident memory, an evaluation suite per agent, incident
  replay against new model versions.
- **Console:** server-sent events instead of polling; a React/TypeScript console; Slack or Teams
  approvals.
- **Operations:** multi-tenant isolation, disaster recovery, dashboards and alarms on the exported
  metrics.
