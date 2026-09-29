# ruff: noqa: E501 - IAM statements and log lines are clearer on one line
"""OpsRelay on one EC2 instance behind HTTPS (API Gateway), with CloudWatch alarms coming in through SQS.

    python deploy/ec2/stack.py up [--env FILE] [--slack-users FILE] [--budget-email ADDRESS]
    python deploy/ec2/stack.py deploy [--env FILE] [--slack-users FILE]   # pull main, reapply settings, restart
    python deploy/ec2/stack.py status
    python deploy/ec2/stack.py down [--delete-data]

What `up` creates (us-east-1 by default, AWS_REGION to change), each only if it is missing:
- DynamoDB table opsrelay (on demand, point-in-time recovery, deletion protection)
- SQS queue opsrelay-alerts (+ dead-letter queue), fed by the EventBridge rule opsrelay-cloudwatch-alarms
- IAM role and instance profile opsrelay-ec2: Bedrock, the table, the queue, secrets under opsrelay/,
  CloudWatch metrics and logs, the ECS services of the demo cluster, and SSM (for `deploy`)
- security group opsrelay (8080 in: requests that skip API Gateway lack the origin secret and are refused)
- an Elastic IP and an arm64 Amazon Linux 2023 instance set up by deploy/ec2/user-data.sh
- API Gateway HTTP API opsrelay, which adds the x-opsrelay-origin header to every request
- the secret opsrelay/origin-secret (random), if it doesn't exist
- with --budget-email: the AWS Budget opsrelay-monthly, $25 a month, emails at $10, $25 and forecast > $25

The service runs with OPSRELAY_ENVIRONMENT=hybrid and the demo workload's service catalog
(deploy/demo/demo.py; `demo.py up` creates the workload itself). Deployment-specific settings go in
--env: a file of OPSRELAY_*=value lines (Slack, sign-in, PagerDuty, Jira). Use secretsmanager:
references for secret values (opsrelay/secrets.py). --slack-users copies a Slack user map to the instance.

`down` keeps the table (and its data) unless --delete-data is given. Secrets are never deleted here.
Cost while up: about $17 a month (t4g.small, Elastic IP, API Gateway and DynamoDB at demo volume).
"""

import argparse
import functools
import json
import os
import secrets
import sys
import time
import urllib.request
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "demo"), str(HERE.parents[1])]
import demo  # noqa: E402

from opsrelay.store.dynamodb import DynamoStore  # noqa: E402

CATALOG, DEMO_CLUSTER, DEMO_ROLE = demo.CATALOG, demo.CLUSTER, demo.ROLE

REGION = os.environ.get("AWS_REGION", "us-east-1")
NAME = TABLE = "opsrelay"
QUEUE, DLQ, RULE = "opsrelay-alerts", "opsrelay-alerts-dlq", "opsrelay-cloudwatch-alarms"
ROLE, ORIGIN_SECRET, BUDGET = "opsrelay-ec2", "opsrelay/origin-secret", "opsrelay-monthly"
INSTANCE_TYPE = os.environ.get("INSTANCE_TYPE", "t4g.small")
AMI = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
EXTRAS = "slack"
TAGS = [{"Key": "Name", "Value": NAME}]

ddb = boto3.client("dynamodb", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)
events = boto3.client("events", region_name=REGION)
iam = boto3.client("iam")
ec2 = boto3.client("ec2", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)
apigw = boto3.client("apigatewayv2", region_name=REGION)
sm = boto3.client("secretsmanager", region_name=REGION)
budgets = boto3.client("budgets", region_name="us-east-1")


@functools.cache
def account() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


def table() -> str:
    try:
        arn = ddb.describe_table(TableName=TABLE)["Table"]["TableArn"]
    except ddb.exceptions.ResourceNotFoundException:
        DynamoStore.create_table(TABLE, REGION)
        arn = ddb.describe_table(TableName=TABLE)["Table"]["TableArn"]
        print(f"Created table {TABLE}")
    ddb.update_continuous_backups(
        TableName=TABLE, PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True}
    )
    if not ddb.describe_table(TableName=TABLE)["Table"].get("DeletionProtectionEnabled"):
        ddb.update_table(TableName=TABLE, DeletionProtectionEnabled=True)
    return arn


def queue_arn(url: str) -> str:
    return sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]


def alerts() -> tuple[str, str]:
    """The alert queue (and its DLQ), and the EventBridge rule that fills it. Returns (url, arn)."""
    dlq = sqs.create_queue(QueueName=DLQ, Attributes={"MessageRetentionPeriod": "1209600"})["QueueUrl"]
    redrive = json.dumps({"deadLetterTargetArn": queue_arn(dlq), "maxReceiveCount": "5"})
    url = sqs.create_queue(QueueName=QUEUE, Attributes={"RedrivePolicy": redrive})["QueueUrl"]
    arn = queue_arn(url)
    rule = events.put_rule(
        Name=RULE,
        Description="CloudWatch alarm state changes into OpsRelay",
        EventPattern=json.dumps(
            {
                "source": ["aws.cloudwatch"],
                "detail-type": ["CloudWatch Alarm State Change"],
                "detail": {"state": {"value": ["ALARM", "OK"]}},
            }
        ),
    )["RuleArn"]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "events.amazonaws.com"},
                "Action": "sqs:SendMessage",
                "Resource": arn,
                "Condition": {"ArnEquals": {"aws:SourceArn": rule}},
            }
        ],
    }
    sqs.set_queue_attributes(QueueUrl=url, Attributes={"Policy": json.dumps(policy)})
    events.put_targets(Rule=RULE, Targets=[{"Id": "alerts", "Arn": arn}])
    return url, arn


def role(table_arn: str, alerts_arn: str) -> None:
    """The instance role, with only what the service calls."""
    statements = [
        {
            "Effect": "Allow",
            "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            "Resource": ["arn:aws:bedrock:*::foundation-model/*", f"arn:aws:bedrock:*:{account()}:inference-profile/*"],
        },
        {
            "Effect": "Allow",
            "Action": [
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:Query",
                "dynamodb:ConditionCheckItem",
            ],
            "Resource": [table_arn, f"{table_arn}/index/*"],
        },
        {
            "Effect": "Allow",
            "Action": [
                "sqs:ReceiveMessage",
                "sqs:DeleteMessage",
                "sqs:ChangeMessageVisibility",
                "sqs:GetQueueAttributes",
            ],
            "Resource": alerts_arn,
        },
        {
            "Effect": "Allow",
            "Action": "secretsmanager:GetSecretValue",
            "Resource": f"arn:aws:secretsmanager:{REGION}:{account()}:secret:opsrelay/*",
        },
        {"Effect": "Allow", "Action": "cloudwatch:GetMetricData", "Resource": "*"},
        {
            "Effect": "Allow",
            "Action": "logs:FilterLogEvents",
            "Resource": f"arn:aws:logs:{REGION}:{account()}:log-group:*",
        },
        {
            "Effect": "Allow",
            "Action": ["ecs:DescribeServices", "ecs:ListTagsForResource", "ecs:TagResource", "ecs:UpdateService"],
            "Resource": f"arn:aws:ecs:{REGION}:{account()}:service/{DEMO_CLUSTER}/*",
        },
        {"Effect": "Allow", "Action": "ecs:DescribeTaskDefinition", "Resource": "*"},
        {"Effect": "Allow", "Action": "iam:PassRole", "Resource": f"arn:aws:iam::{account()}:role/{DEMO_ROLE}"},
    ]
    trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}],
    }
    created = False
    try:
        iam.get_role(RoleName=ROLE)
    except iam.exceptions.NoSuchEntityException:
        iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust), Description="OpsRelay on EC2")
        created = True
    iam.put_role_policy(
        RoleName=ROLE, PolicyName=NAME, PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements})
    )
    iam.attach_role_policy(RoleName=ROLE, PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
    try:
        iam.get_instance_profile(InstanceProfileName=ROLE)
    except iam.exceptions.NoSuchEntityException:
        iam.create_instance_profile(InstanceProfileName=ROLE)
        iam.add_role_to_instance_profile(InstanceProfileName=ROLE, RoleName=ROLE)
        created = True
    if created:
        print(f"Created role and instance profile {ROLE}")
        time.sleep(10)  # let IAM propagate before EC2 uses the profile


def vpc() -> str:
    return ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]["VpcId"]


def security_group() -> str:
    found = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [NAME]}, {"Name": "vpc-id", "Values": [vpc()]}]
    )["SecurityGroups"]
    if found:
        return found[0]["GroupId"]
    sg = ec2.create_security_group(
        GroupName=NAME, Description="OpsRelay: 8080 from API Gateway (origin secret enforced)", VpcId=vpc()
    )["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=sg,
        IpPermissions=[{"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
    )
    return sg


def elastic_ip() -> dict:
    found = ec2.describe_addresses(Filters=[{"Name": "tag:Name", "Values": [NAME]}])["Addresses"]
    if found:
        return found[0]
    address = ec2.allocate_address(Domain="vpc", TagSpecifications=[{"ResourceType": "elastic-ip", "Tags": TAGS}])
    print(f"Allocated Elastic IP {address['PublicIp']}")
    return address


def origin_secret() -> str:
    try:
        return sm.get_secret_value(SecretId=ORIGIN_SECRET)["SecretString"]
    except sm.exceptions.ResourceNotFoundException:
        value = secrets.token_urlsafe(32)
        sm.create_secret(
            Name=ORIGIN_SECRET,
            SecretString=value,
            Description="OpsRelay: header API Gateway adds, so the instance can refuse direct requests",
        )
        print(f"Created secret {ORIGIN_SECRET}")
        return value
    except sm.exceptions.InvalidRequestException:
        sys.exit(
            f"{ORIGIN_SECRET} is scheduled for deletion. Restore it: aws secretsmanager restore-secret --secret-id {ORIGIN_SECRET}"
        )


def find_api() -> dict | None:
    return next((a for a in apigw.get_apis(MaxResults="100")["Items"] if a["Name"] == NAME), None)


def api(ip: str, origin: str) -> str:
    """The HTTPS front door: every path to the instance, plus the origin header. Returns its URL."""
    found = find_api()
    api_id = (
        found["ApiId"]
        if found
        else apigw.create_api(Name=NAME, ProtocolType="HTTP", Description="OpsRelay dashboard and API")["ApiId"]
    )
    routes = {r["RouteKey"]: r for r in apigw.get_routes(ApiId=api_id)["Items"]}
    for key, path in (("ANY /", ""), ("ANY /{proxy+}", "{proxy}")):
        spec = {
            "IntegrationType": "HTTP_PROXY",
            "IntegrationMethod": "ANY",
            "IntegrationUri": f"http://{ip}:8080/{path}",
            "PayloadFormatVersion": "1.0",
            "TimeoutInMillis": 30000,
            "RequestParameters": {"overwrite:header.x-opsrelay-origin": origin},
        }
        if key in routes:
            apigw.update_integration(
                ApiId=api_id, IntegrationId=routes[key]["Target"].removeprefix("integrations/"), **spec
            )
        else:
            integration = apigw.create_integration(ApiId=api_id, **spec)["IntegrationId"]
            apigw.create_route(ApiId=api_id, RouteKey=key, Target=f"integrations/{integration}")
    if not any(s["StageName"] == "$default" for s in apigw.get_stages(ApiId=api_id)["Items"]):
        apigw.create_stage(ApiId=api_id, StageName="$default", AutoDeploy=True)
    return apigw.get_api(ApiId=api_id)["ApiEndpoint"]


def read_env(path: str | None) -> dict[str, str]:
    """OPSRELAY_*=value lines. Values go into a systemd drop-in, so no quotes or backslashes."""
    env: dict[str, str] = {}
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines() if path else [], 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if not sep or not key.startswith("OPSRELAY_") or '"' in value or "\\" in value:
            sys.exit(f"{path}:{n}: expected OPSRELAY_NAME=value (no quotes or backslashes in the value)")
        if (
            any(w in key for w in ("TOKEN", "SECRET", "KEY", "PASSWORD"))
            and value
            and not value.startswith(("secretsmanager:", "arn:aws:secretsmanager:"))
        ):
            print(f"Warning: {key} is a plain value; a secretsmanager: reference keeps it off the instance's disk")
        env[key] = value
    return env


def settings(queue_url: str, public_url: str, env_file: str | None, slack_users: str | None) -> dict[str, str]:
    env = {
        "OPSRELAY_STORE": "dynamodb",
        "OPSRELAY_DYNAMODB_TABLE": TABLE,
        "OPSRELAY_INTAKE_QUEUE_URL": queue_url,
        "OPSRELAY_PUBLIC_URL": public_url,
        "OPSRELAY_ORIGIN_SECRET": f"secretsmanager:{ORIGIN_SECRET}",
        "OPSRELAY_ENVIRONMENT": "hybrid",
        "OPSRELAY_SERVICE_CATALOG": "/opt/opsrelay/catalog.yaml",
    }
    if slack_users:
        env["OPSRELAY_SLACK_USERS"] = "/opt/opsrelay/slack-users.yaml"
    return env | read_env(env_file)


def configure(env: dict[str, str], slack_users: str | None) -> str:
    """Shell that writes the catalog, the Slack user map and the systemd drop-in, then restarts."""
    files = {"/opt/opsrelay/catalog.yaml": CATALOG}
    if slack_users:
        files["/opt/opsrelay/slack-users.yaml"] = Path(slack_users).read_text(encoding="utf-8")
    drop_in = "[Service]\n" + "".join(f'Environment="{k}={v.replace("%", "%%")}"\n' for k, v in env.items())
    files["/etc/systemd/system/opsrelay.service.d/stack.conf"] = drop_in
    lines = ["mkdir -p /etc/systemd/system/opsrelay.service.d"]
    for path, text in files.items():
        lines.append(f"cat > {path} <<'OPSRELAY_EOF'\n{text.rstrip()}\nOPSRELAY_EOF")
    lines += ["chown -R opsrelay:opsrelay /opt/opsrelay", "systemctl daemon-reload", "systemctl restart opsrelay"]
    return "\n".join(lines) + "\n"


def find_instance() -> dict | None:
    for reservation in ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [NAME]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ]
    )["Reservations"]:
        return reservation["Instances"][0]
    return None


def instance(sg: str, user_data: str) -> str:
    found = find_instance()
    if found:
        return found["InstanceId"]
    image = ssm.get_parameter(Name=AMI)["Parameter"]["Value"]
    for attempt in range(6):
        try:
            iid = ec2.run_instances(
                ImageId=image,
                InstanceType=INSTANCE_TYPE,
                MinCount=1,
                MaxCount=1,
                IamInstanceProfile={"Name": ROLE},
                SecurityGroupIds=[sg],
                UserData=user_data,
                MetadataOptions={"HttpTokens": "required"},
                TagSpecifications=[
                    {"ResourceType": "instance", "Tags": TAGS},
                    {"ResourceType": "volume", "Tags": TAGS},
                ],
            )["Instances"][0]["InstanceId"]
            break
        except ClientError as e:  # a brand-new instance profile can take a little longer to be usable
            if e.response["Error"]["Code"] != "InvalidParameterValue" or attempt == 5:
                raise
            time.sleep(10)
    print(f"Launched {iid} ({INSTANCE_TYPE})")
    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    return iid


def budget(email: str) -> None:
    def notify(kind: str, threshold: float, unit: str = "PERCENTAGE") -> dict:
        return {
            "Notification": {
                "NotificationType": kind,
                "ComparisonOperator": "GREATER_THAN",
                "Threshold": threshold,
                "ThresholdType": unit,
            },
            "Subscribers": [{"SubscriptionType": "EMAIL", "Address": email}],
        }

    try:
        budgets.create_budget(
            AccountId=account(),
            Budget={
                "BudgetName": BUDGET,
                "BudgetLimit": {"Amount": "25", "Unit": "USD"},
                "TimeUnit": "MONTHLY",
                "BudgetType": "COST",
            },
            NotificationsWithSubscribers=[notify("ACTUAL", 40), notify("ACTUAL", 100), notify("FORECASTED", 100)],
        )
        print(f"Created budget {BUDGET}: $25 a month")
    except budgets.exceptions.DuplicateRecordException:
        pass


def ping(url: str) -> str:
    """Whether OpsRelay itself answers. Not /ping: API Gateway answers that path on its own."""
    try:
        with urllib.request.urlopen(f"{url}/", timeout=10) as resp:  # noqa: S310 - our https API URL
            page = resp.read(4000).decode(errors="replace")
        return "serving" if "<title>OpsRelay" in page else f"{resp.status}, but not the OpsRelay dashboard"
    except Exception as e:  # noqa: BLE001 - reported, not handled
        return f"no answer ({e})"


def up(args: argparse.Namespace) -> None:
    table_arn = table()
    queue_url, alerts_arn = alerts()
    role(table_arn, alerts_arn)
    sg = security_group()
    address = elastic_ip()
    url = api(address["PublicIp"], origin_secret())
    env = settings(queue_url, url, args.env, args.slack_users)
    user_data = (
        open(HERE / "user-data.sh", encoding="utf-8")
        .read()
        .replace("#!/bin/bash\n", f"#!/bin/bash\nEXTRAS={EXTRAS}\n", 1)
    )
    iid = instance(sg, user_data + "\n" + configure(env, args.slack_users))
    if address.get("InstanceId") != iid:
        ec2.associate_address(AllocationId=address["AllocationId"], InstanceId=iid)
    if args.budget_email:
        budget(args.budget_email)
    print(f"Waiting for {url} (installing takes a few minutes)", end="", flush=True)
    for _ in range(60):
        if ping(url) == "serving":
            break
        print(".", end="", flush=True)
        time.sleep(15)
    print(f"\nOpsRelay: {ping(url)}")
    print(f"Dashboard: {url}   Instance: {iid} ({address['PublicIp']})")


def deploy(args: argparse.Namespace) -> None:
    """Pull main, reinstall, and reapply the settings (for example after adding keys to --env)."""
    found = find_instance()
    url = (find_api() or {}).get("ApiEndpoint")
    if not found or not url:
        sys.exit("Nothing to deploy to: run `up` first")
    queue_url = sqs.get_queue_url(QueueName=QUEUE)["QueueUrl"]
    script = (
        "set -euo pipefail\n"
        "cd /opt/opsrelay/app && sudo -u opsrelay git pull --ff-only\n"
        f'/opt/opsrelay/venv/bin/pip install -q "/opt/opsrelay/app[{EXTRAS}]"\n'
        + configure(settings(queue_url, url, args.env, args.slack_users), args.slack_users)
    )
    iid = found["InstanceId"]
    command = ssm.send_command(InstanceIds=[iid], DocumentName="AWS-RunShellScript", Parameters={"commands": [script]})[
        "Command"
    ]["CommandId"]
    while True:
        time.sleep(5)
        try:
            result = ssm.get_command_invocation(CommandId=command, InstanceId=iid)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if result["Status"] not in ("Pending", "InProgress", "Delayed"):
            break
    print(result["Status"], result["StandardErrorContent"][-2000:] if result["Status"] != "Success" else "")
    time.sleep(5)
    print(f"OpsRelay: {ping(url)}")


def status(_: argparse.Namespace) -> None:
    found, front = find_instance(), find_api()
    print(
        f"Instance: {found['InstanceId']} {found['State']['Name']} {found.get('PublicIpAddress', '')}"
        if found
        else "Instance: none"
    )
    if front:
        print(f"Dashboard: {front['ApiEndpoint']}   OpsRelay: {ping(front['ApiEndpoint'])}")
    try:
        t = ddb.describe_table(TableName=TABLE)["Table"]
        print(
            f"Table: {TABLE} {t['TableStatus']}, {t.get('ItemCount', 0)} items, deletion protection {t.get('DeletionProtectionEnabled')}"
        )
    except ddb.exceptions.ResourceNotFoundException:
        print("Table: none")
    for name in (QUEUE, DLQ):
        try:
            depth = sqs.get_queue_attributes(
                QueueUrl=sqs.get_queue_url(QueueName=name)["QueueUrl"], AttributeNames=["ApproximateNumberOfMessages"]
            )
            print(f"Queue {name}: {depth['Attributes']['ApproximateNumberOfMessages']} waiting")
        except sqs.exceptions.QueueDoesNotExist:
            print(f"Queue {name}: none")


def quietly(what: str, call, **kwargs) -> None:  # noqa: ANN001
    try:
        call(**kwargs)
        print(f"Deleted {what}")
    except ClientError as e:
        print(f"Skipped {what}: {e.response['Error']['Code']}")


def down(args: argparse.Namespace) -> None:
    if front := find_api():
        quietly(f"API {front['ApiEndpoint']}", apigw.delete_api, ApiId=front["ApiId"])
    if found := find_instance():
        ec2.terminate_instances(InstanceIds=[found["InstanceId"]])
        ec2.get_waiter("instance_terminated").wait(InstanceIds=[found["InstanceId"]])
        print(f"Terminated {found['InstanceId']}")
    for address in ec2.describe_addresses(Filters=[{"Name": "tag:Name", "Values": [NAME]}])["Addresses"]:
        quietly(f"Elastic IP {address['PublicIp']}", ec2.release_address, AllocationId=address["AllocationId"])
    for sg in ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [NAME]}, {"Name": "vpc-id", "Values": [vpc()]}]
    )["SecurityGroups"]:
        quietly(f"security group {sg['GroupId']}", ec2.delete_security_group, GroupId=sg["GroupId"])
    quietly("rule target", events.remove_targets, Rule=RULE, Ids=["alerts"])
    quietly(f"rule {RULE}", events.delete_rule, Name=RULE)
    for name in (QUEUE, DLQ):
        try:
            quietly(f"queue {name}", sqs.delete_queue, QueueUrl=sqs.get_queue_url(QueueName=name)["QueueUrl"])
        except sqs.exceptions.QueueDoesNotExist:
            pass
    quietly(
        "role from instance profile", iam.remove_role_from_instance_profile, InstanceProfileName=ROLE, RoleName=ROLE
    )
    quietly(f"instance profile {ROLE}", iam.delete_instance_profile, InstanceProfileName=ROLE)
    quietly("role policy", iam.delete_role_policy, RoleName=ROLE, PolicyName=NAME)
    quietly(
        "SSM policy attachment",
        iam.detach_role_policy,
        RoleName=ROLE,
        PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    )
    quietly(f"role {ROLE}", iam.delete_role, RoleName=ROLE)
    quietly(f"budget {BUDGET}", budgets.delete_budget, AccountId=account(), BudgetName=BUDGET)
    if args.delete_data:
        quietly("deletion protection", ddb.update_table, TableName=TABLE, DeletionProtectionEnabled=False)
        quietly(f"table {TABLE}", ddb.delete_table, TableName=TABLE)
    else:
        print(f"Kept table {TABLE} (--delete-data deletes it)")
    print("Secrets under opsrelay/ are kept; delete them in Secrets Manager if you want them gone.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, fn in (("up", up), ("deploy", deploy), ("status", status), ("down", down)):
        command = commands.add_parser(name)
        command.set_defaults(fn=fn)
        if name in ("up", "deploy"):
            command.add_argument("--env", help="file of OPSRELAY_*=value lines for this deployment")
            command.add_argument("--slack-users", help="YAML map of Slack user ids to OpsRelay users and roles")
        if name == "up":
            command.add_argument("--budget-email", help="create the $25/month budget, emailing this address")
        if name == "down":
            command.add_argument("--delete-data", action="store_true", help="also delete the DynamoDB table")
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
