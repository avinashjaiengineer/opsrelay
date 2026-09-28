"""Settings, read from environment variables prefixed with OPSRELAY_."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Role = Literal["coordinator", "triage", "diagnostics", "remediation", "communications"]
SPECIALISTS: tuple[Role, ...] = ("triage", "diagnostics", "remediation", "communications")


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
    communications_endpoint: str = ""
    a2a_timeout_seconds: int = 600

    # Actions at or below this risk level run without a human. "none" gates every action.
    auto_approve_risk: Literal["none", "low", "medium"] = "none"

    def endpoint_for(self, role: Role) -> str:
        return getattr(self, f"{role}_endpoint")


@lru_cache
def get_settings() -> Settings:
    return Settings()
