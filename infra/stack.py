"""OpsRelay on Amazon Bedrock AgentCore.

Creates:
  * a DynamoDB table shared by every agent (incidents, approvals, audit log, services)
  * five specialist AgentCore Runtimes speaking the A2A protocol
  * one coordinator AgentCore Runtime speaking HTTP, allowed to invoke the specialists
  * the event boundary (context event_intake, default on):
      CloudWatch alarm -> EventBridge rule -> SQS alert queue (+ DLQ) -> intake Lambda -> ingest_alert
      Alertmanager     -> SNS topic --------^
      job ids -> SQS work queue (+ DLQ) -> worker Lambda -> run_job
      every 5 minutes -> recovery Lambda -> recover
All six runtimes run the same container image; OPSRELAY_ROLE selects the agent.

Inbound auth on the coordinator is IAM (SigV4) by default. Context jwt_discovery_url (and
jwt_allowed_clients) switches it to an AgentCore JWT authorizer and forwards the Authorization
header to the app; AgentCore accepts one inbound method, so that requires event_intake=false.
"""

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import aws_sqs as sqs
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
        event_intake = str(ctx("event_intake") or "true").lower() == "true"
        jwt_discovery_url = ctx("jwt_discovery_url")
        if jwt_discovery_url and event_intake:
            raise ValueError(
                "An AgentCore runtime accepts either IAM or JWT inbound auth. The event Lambdas call the "
                "coordinator with IAM, so a JWT authorizer needs -c event_intake=false (or a second coordinator)."
            )

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
            resources=[
                *model_resources(model_id, self.region, self.account),
                # Runbook and incident-memory retrieval (opsrelay.knowledge)
                f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0",
            ],
        )

        def queue_with_dlq(name: str, visibility: Duration) -> sqs.Queue:
            dlq = sqs.Queue(self, f"{name}DeadLetters", retention_period=Duration.days(14), enforce_ssl=True)
            return sqs.Queue(
                self,
                name,
                visibility_timeout=visibility,
                dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=5),
                enforce_ssl=True,
            )

        # Lambdas run up to 15 minutes; SQS visibility must exceed that.
        job_queue = queue_with_dlq("JobQueue", Duration.minutes(16)) if event_intake else None

        def runtime(
            role: str, protocol: agentcore.ProtocolType, extra_env: dict[str, str], **kwargs
        ) -> agentcore.Runtime:  # noqa: ANN003
            rt = agentcore.Runtime(
                self,
                f"{role.capitalize()}Runtime",
                runtime_name=f"opsrelay_{role}",
                description=f"OpsRelay {role} agent",
                agent_runtime_artifact=artifact,
                protocol_configuration=protocol,
                environment_variables={**common_env, "OPSRELAY_ROLE": role, **extra_env},
                tracing_enabled=True,
                **kwargs,
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

        coordinator_auth = {}
        if jwt_discovery_url:
            clients = [c for c in str(ctx("jwt_allowed_clients") or "").split(",") if c]
            coordinator_auth = {
                "authorizer_configuration": agentcore.RuntimeAuthorizerConfiguration.using_jwt(
                    jwt_discovery_url, allowed_clients=clients or None
                ),
                # Forward the token so the app knows who the (already verified) caller is.
                "request_header_configuration": agentcore.RequestHeaderConfiguration(
                    allowlisted_headers=["Authorization"]
                ),
            }
        coordinator = runtime(
            "coordinator",
            agentcore.ProtocolType.HTTP,
            {
                "OPSRELAY_SPECIALIST_TRANSPORT": "a2a",
                **{f"OPSRELAY_{role.upper()}_ENDPOINT": rt.agent_runtime_arn for role, rt in specialists.items()},
                **({"OPSRELAY_JOB_QUEUE_URL": job_queue.queue_url} if job_queue else {}),
            },
            **coordinator_auth,
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

        if event_intake:
            self._event_boundary(coordinator, job_queue, queue_with_dlq)

        CfnOutput(self, "CoordinatorRuntimeArn", value=coordinator.agent_runtime_arn)
        CfnOutput(self, "TableName", value=table.table_name)
        for role, rt in specialists.items():
            CfnOutput(self, f"{role.capitalize()}RuntimeArn", value=rt.agent_runtime_arn)

    def _event_boundary(self, coordinator: agentcore.Runtime, job_queue: sqs.Queue, queue_with_dlq) -> None:  # noqa: ANN001
        """EventBridge and SNS into SQS, and the Lambdas that turn messages into coordinator calls."""
        job_queue.grant_send_messages(coordinator.role)

        alert_queue = queue_with_dlq("AlertQueue", Duration.minutes(16))
        events.Rule(
            self,
            "CloudWatchAlarms",
            description="CloudWatch alarm state changes into OpsRelay",
            event_pattern=events.EventPattern(
                source=["aws.cloudwatch"],
                detail_type=["CloudWatch Alarm State Change"],
                detail={"state": {"value": ["ALARM", "OK"]}},
            ),
            targets=[targets.SqsQueue(alert_queue)],
        )
        alert_topic = sns.Topic(self, "AlertTopic", display_name="OpsRelay alerts (Alertmanager, other sources)")
        alert_topic.add_subscription(subscriptions.SqsSubscription(alert_queue))

        code = lambda_.Code.from_asset(str(Path(__file__).resolve().parent / "lambda"))

        def function(name: str, mode: str) -> lambda_.Function:
            fn = lambda_.Function(
                self,
                name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=lambda_.Architecture.ARM_64,
                handler="handler.handler",
                code=code,
                timeout=Duration.minutes(15),
                memory_size=256,
                environment={"COORDINATOR_ARN": coordinator.agent_runtime_arn, "MODE": mode},
                description=f"OpsRelay {mode}",
            )
            coordinator.grant_invoke_runtime(fn)
            return fn

        intake = function("IntakeFunction", "intake")
        intake.add_event_source(sources.SqsEventSource(alert_queue, batch_size=10, report_batch_item_failures=True))
        worker = function("JobFunction", "jobs")
        worker.add_event_source(sources.SqsEventSource(job_queue, batch_size=1, report_batch_item_failures=True))
        recovery = function("RecoveryFunction", "recover")
        events.Rule(
            self,
            "RecoverySchedule",
            description="Finish interrupted OpsRelay work",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            targets=[targets.LambdaFunction(recovery)],
        )

        CfnOutput(self, "AlertQueueUrl", value=alert_queue.queue_url)
        CfnOutput(self, "AlertTopicArn", value=alert_topic.topic_arn)
        CfnOutput(self, "JobQueueUrl", value=job_queue.queue_url)
