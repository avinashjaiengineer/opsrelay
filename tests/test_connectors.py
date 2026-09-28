"""The AWS environment against mocked CloudWatch, CloudWatch Logs and ECS (moto)."""

import time
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from opsrelay.connectors.aws import AwsEnvironment
from opsrelay.connectors.catalog import ServiceCatalog
from opsrelay.connectors.cloudwatch import CloudWatchLogs, CloudWatchMetrics
from opsrelay.connectors.ecs import EcsDeployer

REGION = "us-east-1"
DIMS = {"ClusterName": "prod", "ServiceName": "checkout-api"}
CATALOG = {
    "services": {
        "checkout-api": {
            "tier": 1,
            "owner_team": "payments",
            "max_replicas": 6,
            "log_group": "/ecs/checkout-api",
            "ecs": {"cluster": "prod", "service": "checkout-api"},
            "healthy_when": {"error_rate_below": 0.01},
            "metrics": {
                "errors": {"namespace": "Shop", "name": "Errors", "stat": "Sum", "dimensions": DIMS},
                "requests": {"namespace": "Shop", "name": "Requests", "stat": "Sum", "dimensions": DIMS},
                "cpu_pct": {"namespace": "AWS/ECS", "name": "CPUUtilization", "stat": "Average", "dimensions": DIMS},
            },
        },
        "payments-db": {"tier": 1, "healthy_when": {"cpu_pct_below": 80}},
    }
}


@pytest.fixture
def aws(monkeypatch):
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(key, "testing")
    with mock_aws():
        clients = {name: boto3.client(name, region_name=REGION) for name in ("cloudwatch", "logs", "ecs")}
        ecs = clients["ecs"]
        ecs.create_cluster(clusterName="prod")
        for _ in range(2):  # revisions :1 and :2
            ecs.register_task_definition(
                family="checkout", containerDefinitions=[{"name": "app", "image": "shop/checkout", "memory": 512}]
            )
        ecs.create_service(cluster="prod", serviceName="checkout-api", taskDefinition="checkout:2", desiredCount=2)
        env = AwsEnvironment(
            ServiceCatalog.from_dict(CATALOG),
            CloudWatchMetrics(clients["cloudwatch"]),
            CloudWatchLogs(clients["logs"]),
            EcsDeployer(clients["ecs"]),
        )
        yield env, clients


def _put(cw, name, value):
    dims = [{"Name": k, "Value": v} for k, v in DIMS.items()]
    cw.put_metric_data(
        Namespace="Shop",
        MetricData=[{"MetricName": name, "Dimensions": dims, "Value": value, "Timestamp": datetime.now(UTC)}],
    )


def test_metrics_from_cloudwatch(aws):
    env, clients = aws
    _put(clients["cloudwatch"], "Errors", 30)
    _put(clients["cloudwatch"], "Requests", 1000)
    m = env.metrics("checkout-api")
    assert (m["error_rate"], m["healthy"], m["replicas"], m["source"]) == (0.03, False, 2, "cloudwatch")

    _put(clients["cloudwatch"], "Requests", 99000)  # error rate falls to 30/100000
    assert env.metrics("checkout-api")["healthy"] is True


def test_no_data_is_not_healthy(aws):
    env, _ = aws
    assert env.metrics("payments-db")["healthy"] is False
    assert {s["service"]: s["tier"] for s in env.health_overview()} == {"checkout-api": 1, "payments-db": 1}


def test_logs_from_cloudwatch_logs(aws):
    env, clients = aws
    logs = clients["logs"]
    logs.create_log_group(logGroupName="/ecs/checkout-api")
    logs.create_log_stream(logGroupName="/ecs/checkout-api", logStreamName="app")
    now = int(time.time() * 1000)
    logs.put_log_events(
        logGroupName="/ecs/checkout-api",
        logStreamName="app",
        logEvents=[
            {"timestamp": now, "message": "ERROR NullPointerException"},
            {"timestamp": now, "message": "INFO ok"},
        ],
    )
    assert env.logs("checkout-api") == ["ERROR NullPointerException", "INFO ok"]
    assert "No log group" in env.logs("payments-db")[0]


def test_service_info_and_deployments_from_ecs(aws):
    env, _ = aws
    info = env.service_info("checkout-api")
    assert (info["version"], info["replicas"], info["tier"]) == ("checkout:2", 2, 1)
    assert [d["version"] for d in env.deployments("checkout-api")] == ["checkout:1", "checkout:2"]
    assert env.deployments("payments-db") == []


def test_actions_on_ecs_and_reconcile(aws):
    env, _ = aws
    assert env.reconcile("scale_service", "checkout-api", {"replicas": 4}, "inc-1:scale:x") == "not_applied"

    result = env.execute("scale_service", "checkout-api", {"replicas": 4}, idempotency_key="inc-1:scale:x")
    assert result == {"ok": True, "detail": "Scaled checkout-api to 4 tasks"}
    assert env.service_info("checkout-api")["replicas"] == 4
    assert env.reconcile("scale_service", "checkout-api", {"replicas": 4}, "inc-1:scale:x") == "applied"

    assert env.execute("rollback_deployment", "checkout-api", {}, idempotency_key="inc-1:rb:y")["ok"]
    assert env.service_info("checkout-api")["version"] == "checkout:1"
    assert env.reconcile("rollback_deployment", "checkout-api", {}, "inc-1:rb:y") == "applied"
    assert env.execute("scale_service", "checkout-api", {"replicas": 60})["ok"] is False  # above max_replicas


def test_intent_without_effect_is_unknown_not_repeated(aws):
    """The process died after recording the intent but before the change: ask a person."""
    env, clients = aws
    svc = clients["ecs"].describe_services(cluster="prod", services=["checkout-api"])["services"][0]
    clients["ecs"].tag_resource(
        resourceArn=svc["serviceArn"],
        tags=[
            {"key": "opsrelay:last-change", "value": "inc-1:rb:z"},
            {"key": "opsrelay:target", "value": "taskDefinition=checkout:1"},
        ],
    )
    assert env.reconcile("rollback_deployment", "checkout-api", {}, "inc-1:rb:z") == "unknown"


def test_services_without_a_deploy_connector_are_never_changed(aws):
    env, _ = aws
    result = env.execute("restart_service", "payments-db", {})
    assert result["ok"] is False and "manually" in result["detail"]
    assert env.reconcile("restart_service", "payments-db", {}, "k") == "unknown"


def test_the_example_catalog_loads():
    catalog = ServiceCatalog.load(str(Path(__file__).parent.parent / "deploy" / "catalog.example.yaml"))
    assert catalog.get("checkout-api").ecs == {"cluster": "prod", "service": "checkout-api"}
    assert catalog.get("payments-db").cpu_pct_below == 80
    with pytest.raises(KeyError, match="Known services"):
        catalog.get("nope")
