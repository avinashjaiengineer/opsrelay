"""OpsRelay on Amazon Bedrock AgentCore.

Creates:
  * a DynamoDB table shared by every agent (incidents, approvals, audit log, services)
  * five specialist AgentCore Runtimes speaking the A2A protocol
  * one coordinator AgentCore Runtime speaking HTTP, allowed to invoke the specialists
All six run the same container image; OPSRELAY_ROLE selects the agent.
"""

from pathlib import Path

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from constructs import Construct

ROOT = Path(__file__).resolve().parent.parent
SPECIALISTS = ("triage", "diagnostics", "remediation", "verification", "communications")

# Least privilege: which item prefixes each runtime may write (dynamodb:LeadingKeys). Every runtime
# may read the table; writes mirror the agent contracts. Specialists write only their incident's
# items (results and audit events); remediation also writes approvals, executions and services,
# because an action the policy allows without a person runs inside its proposal; only the
# coordinator writes jobs, dead letters and policy versions.
INCIDENT_KEYS = ["INC#*"]
WRITABLE_KEYS = {
    "coordinator": ["INC#*", "APR#*", "REC#*", "SVC#*"],
    "triage": INCIDENT_KEYS,
    "diagnostics": INCIDENT_KEYS,
    "remediation": ["INC#*", "APR#*", "REC#execution#*", "SVC#*"],
    "verification": INCIDENT_KEYS,
    "communications": INCIDENT_KEYS,
}
CROSS_REGION_PREFIXES = ("global.", "us.", "eu.", "apac.", "jp.", "au.", "ca.")


def model_resources(model_id: str, region: str, account: str) -> list[str]:
    """Only the configured model: its inference profile, and the foundation model it routes to."""
    prefix = next((p for p in CROSS_REGION_PREFIXES if model_id.startswith(p)), None)
    if prefix is None:
        return [f"arn:aws:bedrock:{region}::foundation-model/{model_id}"]
    base = model_id[len(prefix) :]
    return [
        f"arn:aws:bedrock:{region}:{account}:inference-profile/{model_id}",
        f"arn:aws:bedrock:*::foundation-model/{base}",  # the regions the profile routes to
        f"arn:aws:bedrock:::foundation-model/{base}",  # global profiles
    ]


class OpsRelayStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:  # noqa: ANN003
        super().__init__(scope, construct_id, **kwargs)

        ctx = self.node.try_get_context
        model_provider = ctx("model_provider") or "bedrock"
        model_id = ctx("model_id") or "global.amazon.nova-2-lite-v1:0"
        retain_data = str(ctx("retain_data") or "true").lower() == "true"

        table = dynamodb.TableV2(
            self,
            "State",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            global_secondary_indexes=[
                dynamodb.GlobalSecondaryIndexPropsV2(
                    index_name="gsi1",
                    partition_key=dynamodb.Attribute(name="gsi1pk", type=dynamodb.AttributeType.STRING),
                    sort_key=dynamodb.Attribute(name="gsi1sk", type=dynamodb.AttributeType.STRING),
                )
            ],
            billing=dynamodb.Billing.on_demand(),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN if retain_data else RemovalPolicy.DESTROY,
        )

        artifact = agentcore.AgentRuntimeArtifact.from_asset(
            str(ROOT),
            platform=ecr_assets.Platform.LINUX_ARM64,  # AgentCore Runtime runs arm64
            exclude=[".git", ".venv", "infra", "tests", "docs", "*.db", "*.db-*", "**/__pycache__"],
        )

        common_env = {
            "OPSRELAY_STORE": "dynamodb",
            "OPSRELAY_DYNAMODB_TABLE": table.table_name,
            "OPSRELAY_AWS_REGION": self.region,
            "OPSRELAY_MODEL_PROVIDER": model_provider,
            "OPSRELAY_BEDROCK_MODEL_ID": model_id,
        }
        for key in ("auth_mode", "oidc_issuer", "oidc_audience", "dev_users"):
            if ctx(key):
                common_env[f"OPSRELAY_{key.upper()}"] = str(ctx(key))
        model_access = iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=model_resources(model_id, self.region, self.account),
        )

        def runtime(role: str, protocol: agentcore.ProtocolType, extra_env: dict[str, str]) -> agentcore.Runtime:
            rt = agentcore.Runtime(
                self,
                f"{role.capitalize()}Runtime",
                runtime_name=f"opsrelay_{role}",
                description=f"OpsRelay {role} agent",
                agent_runtime_artifact=artifact,
                protocol_configuration=protocol,
                environment_variables={**common_env, "OPSRELAY_ROLE": role, **extra_env},
                tracing_enabled=True,
            )
            table.grant_read_data(rt.role)
            rt.role.add_to_principal_policy(
                iam.PolicyStatement(
                    actions=["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:ConditionCheckItem"],
                    resources=[table.table_arn],
                    conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": WRITABLE_KEYS[role]}},
                )
            )
            rt.role.add_to_principal_policy(model_access)
            return rt

        specialists = {role: runtime(role, agentcore.ProtocolType.A2_A, {}) for role in SPECIALISTS}

        coordinator = runtime(
            "coordinator",
            agentcore.ProtocolType.HTTP,
            {
                "OPSRELAY_SPECIALIST_TRANSPORT": "a2a",
                **{f"OPSRELAY_{role.upper()}_ENDPOINT": rt.agent_runtime_arn for role, rt in specialists.items()},
            },
        )
        for rt in specialists.values():
            rt.grant_invoke_runtime(coordinator.role)
        # Secrets (e.g. the dev users file) live under opsrelay/ in Secrets Manager.
        coordinator.role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:opsrelay/*"],
            )
        )

        CfnOutput(self, "CoordinatorRuntimeArn", value=coordinator.agent_runtime_arn)
        CfnOutput(self, "TableName", value=table.table_name)
        for role, rt in specialists.items():
            CfnOutput(self, f"{role.capitalize()}RuntimeArn", value=rt.agent_runtime_arn)
