# ruff: noqa: E501 - the service catalog YAML below is clearer on one line per metric
"""A real workload for OpsRelay: shop-api on ECS Fargate, a CloudWatch alarm, and a bad release.

    python deploy/demo/demo.py up        # create everything; prints the service catalog entry
    python deploy/demo/demo.py break     # deploy release 1.1, which fails 40% of checkouts
    python deploy/demo/demo.py status    # running task definition, recent metrics, alarm state
    python deploy/demo/demo.py down      # delete everything this script created

What `up` creates (us-east-1 by default, AWS_REGION to change): log group /ecs/shop-api, IAM role
opsrelay-demo-execution, ECS cluster opsrelay-demo, a security group with no inbound rules, task
definition family shop-api, a Fargate (arm64, 0.25 vCPU) service with a public IP to pull the image,
and the alarm "shop-api error rate above 5%". The alarm reaches OpsRelay through the existing
EventBridge rule for CloudWatch alarms. Cost while running: roughly $0.30 a day.

`break` records the running task definition in the service's `opsrelay:previous` tag, as a deploy
pipeline would, so OpsRelay's rollback returns to exactly that release.
"""

import json
import os
import sys
import time
from pathlib import Path

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
CLUSTER, SERVICE, FAMILY = "opsrelay-demo", "shop-api", "shop-api"
LOG_GROUP, ROLE, SG_NAME = "/ecs/shop-api", "opsrelay-demo-execution", "opsrelay-demo"
ALARM = "shop-api error rate above 5%"
IMAGE = "public.ecr.aws/docker/library/python:3.12-alpine"
SOURCE = (Path(__file__).parent / "shop_api.py").read_text(encoding="utf-8")

ecs = boto3.client("ecs", region_name=REGION)
ec2 = boto3.client("ec2", region_name=REGION)
iam = boto3.client("iam")
logs = boto3.client("logs", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)

CATALOG = f"""services:
  shop-api:
    description: Storefront checkout API (the OpsRelay demo workload on ECS Fargate)
    tier: 1
    owner_team: storefront
    max_replicas: 4
    log_group: {LOG_GROUP}
    ecs: {{cluster: {CLUSTER}, service: {SERVICE}}}
    window_minutes: 2
    settle_seconds: 75
    healthy_when: {{error_rate_below: 0.02, p99_latency_ms_below: 1000}}
    metrics:
      errors:   {{namespace: OpsRelay/Demo, name: Errors, stat: Sum, dimensions: {{Service: shop-api}}}}
      requests: {{namespace: OpsRelay/Demo, name: Requests, stat: Sum, dimensions: {{Service: shop-api}}}}
      p99_latency_ms: {{namespace: OpsRelay/Demo, name: LatencyP99Ms, stat: Maximum, dimensions: {{Service: shop-api}}}}
      cpu_pct:    {{namespace: AWS/ECS, name: CPUUtilization, stat: Average, dimensions: {{ClusterName: {CLUSTER}, ServiceName: {SERVICE}}}}}
      memory_pct: {{namespace: AWS/ECS, name: MemoryUtilization, stat: Average, dimensions: {{ClusterName: {CLUSTER}, ServiceName: {SERVICE}}}}}
"""


def role_arn() -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}
        ],
    }
    try:
        return iam.get_role(RoleName=ROLE)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        iam.attach_role_policy(
            RoleName=ROLE, PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
        )
        time.sleep(10)  # let the new role propagate before ECS uses it
        return arn


def network() -> tuple[list[str], str]:
    vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
    subnets = [
        s["SubnetId"] for s in ec2.describe_subnets(Filters=[{"Name": "default-for-az", "Values": ["true"]}])["Subnets"]
    ][:3]
    found = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [SG_NAME]}, {"Name": "vpc-id", "Values": [vpc]}]
    )["SecurityGroups"]
    sg = (
        found[0]["GroupId"]
        if found
        else ec2.create_security_group(GroupName=SG_NAME, Description="OpsRelay demo: outbound only", VpcId=vpc)[
            "GroupId"
        ]
    )
    return subnets, sg


def register(version: str, fault_rate: float) -> str:
    td = ecs.register_task_definition(
        family=FAMILY,
        requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc",
        cpu="256",
        memory="512",
        runtimePlatform={"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        executionRoleArn=role_arn(),
        containerDefinitions=[
            {
                "name": "app",
                "image": IMAGE,
                "essential": True,
                "command": ["python", "-c", SOURCE],
                "environment": [
                    {"name": "APP_VERSION", "value": version},
                    {"name": "FAULT_RATE", "value": str(fault_rate)},
                    {"name": "SERVICE", "value": SERVICE},
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {"awslogs-group": LOG_GROUP, "awslogs-region": REGION, "awslogs-stream-prefix": "app"},
                },
            }
        ],
    )["taskDefinition"]
    return f"{td['family']}:{td['revision']}"


def service() -> dict | None:
    found = ecs.describe_services(cluster=CLUSTER, services=[SERVICE]).get("services", [])
    return found[0] if found and found[0]["status"] == "ACTIVE" else None


def short(arn: str) -> str:
    return arn.rsplit("/", 1)[-1]


def up() -> None:
    try:
        logs.create_log_group(logGroupName=LOG_GROUP)
        logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=3)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    ecs.create_cluster(clusterName=CLUSTER)
    subnets, sg = network()
    if service() is None:
        td = register("1.0", 0.0)
        ecs.create_service(
            cluster=CLUSTER,
            serviceName=SERVICE,
            taskDefinition=td,
            desiredCount=1,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {"subnets": subnets, "securityGroups": [sg], "assignPublicIp": "ENABLED"}
            },
            deploymentConfiguration={"minimumHealthyPercent": 100, "maximumPercent": 200},
            propagateTags="SERVICE",
        )
        print(f"Created service {SERVICE} running {td}")
    metrics = [
        {
            "Id": key,
            "MetricStat": {
                "Metric": {
                    "Namespace": "OpsRelay/Demo",
                    "MetricName": name,
                    "Dimensions": [{"Name": "Service", "Value": SERVICE}],
                },
                "Period": 60,
                "Stat": "Sum",
            },
            "ReturnData": False,
        }
        for key, name in (("errors", "Errors"), ("requests", "Requests"))
    ]
    cw.put_metric_alarm(
        AlarmName=ALARM,
        AlarmDescription="shop-api (ECS service opsrelay-demo/shop-api): share of checkouts failing above 5%",
        Metrics=[
            *metrics,
            {"Id": "rate", "Expression": "errors / requests", "Label": "error rate", "ReturnData": True},
        ],
        EvaluationPeriods=2,
        DatapointsToAlarm=2,
        Threshold=0.05,
        ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching",
    )
    print(f"Alarm '{ALARM}' ready. Waiting for the service to be stable...")
    ecs.get_waiter("services_stable").wait(cluster=CLUSTER, services=[SERVICE])
    print("Stable. Service catalog entry for OpsRelay (OPSRELAY_SERVICE_CATALOG):\n")
    print(CATALOG)


def break_it() -> None:
    svc = service()
    if svc is None:
        sys.exit("run `up` first")
    current = short(svc["taskDefinition"])
    bad = register("1.1", 0.4)
    ecs.tag_resource(resourceArn=svc["serviceArn"], tags=[{"key": "opsrelay:previous", "value": current}])
    ecs.update_service(cluster=CLUSTER, service=SERVICE, taskDefinition=bad)
    print(f"Deployed release 1.1 ({bad}); 40% of checkouts will fail. Rollback target: {current}.")
    print("The alarm should fire in about 3 minutes.")


def status() -> None:
    svc = service()
    if svc is None:
        return print("shop-api is not running (run `up`)")
    print(f"service {SERVICE}: {short(svc['taskDefinition'])}, running {svc['runningCount']}/{svc['desiredCount']}")
    for d in svc.get("deployments", []):
        print(f"  deployment {d['status']}: {short(d['taskDefinition'])} ({d.get('rolloutState', '')})")
    end = time.time()
    resp = cw.get_metric_data(
        MetricDataQueries=[
            {
                "Id": key.lower(),
                "MetricStat": {
                    "Metric": {
                        "Namespace": "OpsRelay/Demo",
                        "MetricName": key,
                        "Dimensions": [{"Name": "Service", "Value": SERVICE}],
                    },
                    "Period": 60,
                    "Stat": "Sum",
                },
            }
            for key in ("Requests", "Errors")
        ],
        StartTime=end - 300,
        EndTime=end + 60,
    )
    series = {r["Id"]: r["Values"] for r in resp["MetricDataResults"]}
    for i, (req, err) in enumerate(zip(series.get("requests", []), series.get("errors", []), strict=False)):
        print(
            f"  {i} min ago: {int(req)} requests, {int(err)} errors ({err / req:.0%})"
            if req
            else f"  {i} min ago: no data"
        )
    alarm = cw.describe_alarms(AlarmNames=[ALARM])["MetricAlarms"]
    print(f"alarm: {alarm[0]['StateValue'] if alarm else 'missing'}")


def down() -> None:
    cw.delete_alarms(AlarmNames=[ALARM])
    if service() is not None:
        ecs.update_service(cluster=CLUSTER, service=SERVICE, desiredCount=0)
        ecs.delete_service(cluster=CLUSTER, service=SERVICE, force=True)
        print("Deleting the service...")
        ecs.get_waiter("services_inactive").wait(cluster=CLUSTER, services=[SERVICE])
    for arn in ecs.list_task_definitions(familyPrefix=FAMILY)["taskDefinitionArns"]:
        ecs.deregister_task_definition(taskDefinition=arn)
    try:
        ecs.delete_cluster(cluster=CLUSTER)
    except ecs.exceptions.ClientException as e:
        print(f"cluster: {e}")
    try:
        logs.delete_log_group(logGroupName=LOG_GROUP)
    except logs.exceptions.ResourceNotFoundException:
        pass
    _subnets, sg = network()
    for _ in range(12):  # the task's network interface takes a moment to go away
        try:
            ec2.delete_security_group(GroupId=sg)
            break
        except ec2.exceptions.ClientError:
            time.sleep(10)
    try:
        iam.detach_role_policy(
            RoleName=ROLE, PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
        )
        iam.delete_role(RoleName=ROLE)
    except iam.exceptions.NoSuchEntityException:
        pass
    print("Removed the demo workload. (Remove shop-api from the OpsRelay service catalog too.)")


if __name__ == "__main__":
    commands = {"up": up, "break": break_it, "status": status, "down": down}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    commands[sys.argv[1]]()
