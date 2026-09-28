"""Settings, read from environment variables prefixed with OPSRELAY_."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Role = Literal["coordinator", "triage", "diagnostics", "remediation", "verification", "communications"]
SPECIALISTS: tuple[Role, ...] = ("triage", "diagnostics", "remediation", "verification", "communications")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="OPSRELAY_", extra="ignore")

    # Which agent this process runs when started with `python -m opsrelay.runtime`.
    role: Role = "coordinator"

    # "bedrock" runs agents on a model in Amazon Bedrock (Amazon Nova by default, or Claude).
    # "offline" runs deterministic scripted agents: no AWS account needed (demo, tests, CI).
    model_provider: Literal["bedrock", "offline"] = "offline"
    # Bedrock model or inference-profile id. Any Converse model with tool use works. Amazon Nova
    # needs no AWS Marketplace subscription; Claude does (e.g. "global.anthropic.claude-opus-5").
    # Cross-region inference profiles start with "global." or "us.".
    bedrock_model_id: str = "global.amazon.nova-2-lite-v1:0"
    aws_region: str = "us-east-1"
    max_tokens: int = 16000

    # What the agents observe and act on: "simulated" (scenarios, no real systems) or "aws"
    # (CloudWatch metrics and logs, ECS deployments; see opsrelay/connectors).
    environment: Literal["simulated", "aws"] = "simulated"
    service_catalog: str = ""

    # "sqlite" for local development; "dynamodb" on AWS (all runtimes share one table).
    store: Literal["sqlite", "dynamodb"] = "sqlite"
    sqlite_path: str = "opsrelay.db"
    dynamodb_table: str = "opsrelay"
    # DynamoDB Local (e.g. http://dynamodb:8000). Uses dummy credentials, so your real AWS
    # credentials are only used for Bedrock. Leave empty on AWS.
    dynamodb_endpoint: str = ""

    # How the coordinator reaches the specialists.
    #   "local": specialists run in the coordinator's process (development, tests).
    #   "a2a":   specialists are remote A2A agents, e.g. separate AgentCore Runtimes.
    specialist_transport: Literal["local", "a2a"] = "local"
    # For "a2a": an AgentCore runtime ARN or a plain http(s) A2A endpoint per specialist.
    triage_endpoint: str = ""
    diagnostics_endpoint: str = ""
    remediation_endpoint: str = ""
    verification_endpoint: str = ""
    communications_endpoint: str = ""
    a2a_timeout_seconds: int = 600

    # Delegation resilience: each call to a specialist gets `agent_timeout_seconds`, failed calls
    # are retried with exponential backoff, and after `breaker_failure_threshold` consecutive
    # failures the agent's circuit opens for `breaker_recovery_seconds`. When every attempt
    # fails, the request goes to the dead-letter queue and the incident to a human.
    agent_timeout_seconds: float = 300
    agent_max_attempts: int = 3
    retry_backoff_seconds: float = 1.0
    breaker_failure_threshold: int = 5
    breaker_recovery_seconds: float = 30

    # An execution's lease: if the executor running an action stops, another may take over (and
    # reconcile with the environment) once the lease expires.
    execution_lease_seconds: float = 300

    # Durable background work (opsrelay.jobs): a job's lease is renewed while it runs; if its
    # worker stops, another takes the job over once the lease expires. A job that fails
    # `job_max_attempts` times is dead-lettered and its incident handed to a person.
    job_lease_seconds: float = 900
    # On AWS: an SQS queue that carries job ids to a Lambda, which calls the coordinator's run_job
    # action (see opsrelay.jobs). Empty: a worker thread in the coordinator polls the store.
    job_queue_url: str = ""
    job_max_attempts: int = 3
    worker_poll_seconds: float = 1.0
    recovery_interval_seconds: float = 30

    # Alert intake (opsrelay.intake): a new alert on a service that already has an open incident
    # opened within this window is correlated with it rather than opening another. With
    # `intake_queue_url` set, a consumer thread reads alerts from that SQS queue (EventBridge rule
    # for CloudWatch alarms, or Alertmanager via SNS). `webhook_token` protects /alerts/alertmanager.
    correlation_window_minutes: float = 30
    intake_queue_url: str = ""
    webhook_token: str = ""

    # Who may call the coordinator API (opsrelay.auth, opsrelay.rbac): "none" (development; the
    # approver is an unverified name), "dev" (named users with bearer tokens from `dev_users`, a
    # YAML file path or Secrets Manager reference) or "oidc" (JWTs, e.g. Amazon Cognito).
    auth_mode: Literal["none", "dev", "oidc"] = "none"
    dev_users: str = ""
    oidc_issuer: str = ""  # e.g. https://cognito-idp.us-east-1.amazonaws.com/us-east-1_AbCdEf
    oidc_audience: str = ""  # the app client id; empty skips the audience check
    oidc_jwks_url: str = ""  # default: <issuer>/.well-known/jwks.json
    oidc_roles_claim: str = "cognito:groups"
    oidc_name_claim: str = "email"

    # Shared secret the coordinator presents to specialist A2A servers (Authorization: Bearer),
    # for deployments without SigV4 (local, EC2, Docker Compose). `opsrelay up` generates one per
    # run. May be a Secrets Manager ARN (see opsrelay.secrets).
    a2a_token: str = ""

    # Remediation policy (see opsrelay/policies.yaml). Empty: the built-in policy.
    policy_file: str = ""

    # Retrieval (opsrelay.knowledge, opsrelay.runbooks, opsrelay.memory): "auto" embeds with
    # Amazon Titan Text Embeddings v2 when the agents run on Bedrock, else a local lexical embedder.
    # `runbook_dir` adds your own Markdown runbooks (same id overrides a built-in).
    embeddings: Literal["auto", "bedrock", "lexical"] = "auto"
    runbook_dir: str = ""

    def endpoint_for(self, role: Role) -> str:
        return getattr(self, f"{role}_endpoint")


@lru_cache
def get_settings() -> Settings:
    return Settings()
