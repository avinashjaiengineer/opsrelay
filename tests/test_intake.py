"""Event-driven intake: parsing, deduplication, correlation, the webhook and the SQS consumer."""

import json

import boto3
import pytest
from moto import mock_aws
from starlette.testclient import TestClient

from opsrelay.config import get_settings
from opsrelay.intake import alerts
from opsrelay.intake.sqs import SqsIntake
from opsrelay.lifecycle import Status, transition
from opsrelay.runtime import coordinator

SERVICES = ["checkout-api", "auth-service", "inventory-service"]


def cloudwatch_event(state="ALARM", name="checkout-api-5xx", service="checkout-api"):
    """An EventBridge "CloudWatch Alarm State Change" event, as AWS sends it."""
    return {
        "version": "0",
        "id": "c4c1c1c9-6542-e61b-6ef0-8c4d36933a92",
        "detail-type": "CloudWatch Alarm State Change",
        "source": "aws.cloudwatch",
        "account": "123456789012",
        "time": "2026-09-28T12:00:00Z",
        "region": "us-east-1",
        "resources": [f"arn:aws:cloudwatch:us-east-1:123456789012:alarm:{name}"],
        "detail": {
            "alarmName": name,
            "state": {
                "value": state,
                "reason": "Threshold Crossed: 1 datapoint [0.23] > 0.05",
                "timestamp": "2026-09-28T12:00:00Z",
            },
            "previousState": {"value": "OK"},
            "configuration": {
                "description": "5xx error rate above 5%",
                "metrics": [
                    {
                        "id": "m1",
                        "metricStat": {
                            "metric": {
                                "namespace": "Shop",
                                "name": "ErrorRate",
                                "dimensions": {"ServiceName": service},
                            },
                            "period": 60,
                            "stat": "Average",
                        },
                    }
                ],
            },
        },
    }


def alertmanager(status="firing", alertname="HighMemory", service="auth-service", fingerprint="a1b2c3"):
    return {
        "version": "4",
        "groupKey": '{}:{alertname="HighMemory"}',
        "status": status,
        "receiver": "opsrelay",
        "alerts": [
            {
                "status": status,
                "labels": {"alertname": alertname, "service": service, "severity": "critical"},
                "annotations": {"summary": f"{service} memory above 90%", "description": "Pods are being OOMKilled"},
                "startsAt": "2026-09-28T12:00:00Z",
                "generatorURL": "http://prometheus/graph",
                "fingerprint": fingerprint,
            }
        ],
    }


def test_parse_cloudwatch_eventbridge_event():
    [a] = alerts.parse(cloudwatch_event(), SERVICES)
    assert (a.source, a.status, a.service, a.title) == (
        "cloudwatch",
        "firing",
        "checkout-api",
        "CloudWatch alarm checkout-api-5xx",
    )
    assert a.description == "5xx error rate above 5%"
    [ok] = alerts.parse(cloudwatch_event(state="OK"), SERVICES)
    assert ok.status == "resolved" and ok.fingerprint == a.fingerprint


def test_parse_classic_alarm_via_sns():
    envelope = {
        "Type": "Notification",
        "Message": json.dumps(
            {
                "AlarmName": "inventory-service-cpu",
                "AlarmArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:inventory-service-cpu",
                "NewStateValue": "ALARM",
                "NewStateReason": "CPU above 85%",
                "Trigger": {"Dimensions": [{"name": "ServiceName", "value": "inventory-service"}]},
            }
        ),
    }
    [a] = alerts.parse(envelope, SERVICES)
    assert (a.service, a.status, a.description) == ("inventory-service", "firing", "CPU above 85%")


def test_parse_alertmanager_and_reject_garbage():
    [a] = alerts.parse(alertmanager(), SERVICES)
    assert (a.source, a.service, a.severity, a.title) == (
        "alertmanager",
        "auth-service",
        "critical",
        "auth-service memory above 90%",
    )
    for bad in ("not json", "[1, 2]", {"hello": "world"}):
        with pytest.raises(alerts.UnrecognizedAlert):
            alerts.parse(bad, SERVICES)


def test_new_alert_opens_an_incident_and_queues_its_coordination(service):
    [result] = service.ingest(cloudwatch_event())
    assert result["outcome"] == "created" and result["job_id"]
    incident = service.store.get_incident(result["incident_id"])
    assert (incident["source"], incident["service"], incident["status"]) == ("cloudwatch", "checkout-api", "open")
    assert incident["alert"]["labels"]["alarm"] == "checkout-api-5xx"
    assert service.store.get_record("job", result["job_id"])["status"] == "queued"


def test_repeats_are_deduplicated_and_resolutions_noted(service):
    [first] = service.ingest(cloudwatch_event())
    [again] = service.ingest(cloudwatch_event())
    [ok] = service.ingest(cloudwatch_event(state="OK"))

    assert (again["outcome"], again["incident_id"]) == ("deduplicated", first["incident_id"])
    assert (ok["outcome"], ok["incident_id"]) == ("resolved_noted", first["incident_id"])
    incident = service.store.get_incident(first["incident_id"])
    assert incident["alerts_count"] == 2 and incident["source_resolved_at"]
    assert len(service.list_incidents()) == 1
    kinds = [e["kind"] for e in service.store.list_events(first["incident_id"])]
    assert "alert.duplicate" in kinds and "alert.resolved" in kinds


def test_alerts_on_the_same_service_are_correlated(service):
    [latency] = service.ingest(cloudwatch_event(name="checkout-api-latency"))
    [errors] = service.ingest(cloudwatch_event(name="checkout-api-5xx"))
    [other] = service.ingest(alertmanager())  # a different service: its own incident

    assert errors == {"outcome": "correlated", "incident_id": latency["incident_id"]}
    assert other["outcome"] == "created" and other["incident_id"] != latency["incident_id"]
    assert len(service.store.get_incident(latency["incident_id"])["fingerprints"]) == 2
    # The correlated alert's fingerprint now points at that incident too: its repeats dedupe there.
    assert service.ingest(cloudwatch_event(name="checkout-api-5xx"))[0]["incident_id"] == latency["incident_id"]


def test_after_an_incident_closes_the_same_alert_opens_a_new_one(service):
    [first] = service.ingest(cloudwatch_event())
    for status, actor in ((Status.TRIAGING, "coordinator"), (Status.ESCALATED, "coordinator")):
        transition(service.store, first["incident_id"], status, actor=actor)
    [second] = service.ingest(cloudwatch_event())
    assert second["outcome"] == "created" and second["incident_id"] != first["incident_id"]


def test_a_copy_arriving_while_the_incident_is_being_created_is_a_duplicate(service):
    from opsrelay.store.base import new_record

    fp = alerts.parse(cloudwatch_event(), SERVICES)[0].fingerprint
    service.store.put_record(
        new_record("alert_key", fp, "open", incident_id="inc-00000000ab")
    )  # claimed, not created yet
    [result] = service.ingest(cloudwatch_event())
    assert result["outcome"] == "deduplicated" and result["note"] == "incident being created"
    assert service.list_incidents() == []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(coordinator, "_service", None)
    with TestClient(coordinator.app) as c:
        yield c


def test_webhook_needs_its_token(client, monkeypatch):
    assert client.post("/alerts", json=alertmanager()).status_code == 404  # disabled without a token
    monkeypatch.setenv("OPSRELAY_WEBHOOK_TOKEN", "hook-secret")
    get_settings.cache_clear()
    assert client.post("/alerts", json=alertmanager()).status_code == 401
    bad = client.post("/alerts", json={"nope": 1}, headers={"Authorization": "Bearer hook-secret"})
    assert bad.status_code == 400
    ok = client.post("/alerts", json=alertmanager(), headers={"Authorization": "Bearer hook-secret"})
    assert ok.status_code == 200 and ok.json()["results"][0]["outcome"] == "created"


def test_ingest_alert_action(client):
    body = client.post("/invocations", json={"action": "ingest_alert", "message": cloudwatch_event()}).json()
    assert body["results"][0]["outcome"] == "created"


def test_sqs_consumer_routes_and_leaves_bad_messages_for_the_dlq(service, monkeypatch):
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(key, "testing")
    with mock_aws():
        sqs = boto3.client("sqs", region_name="us-east-1")
        url = sqs.create_queue(QueueName="opsrelay-alerts")["QueueUrl"]
        sqs.send_message(QueueUrl=url, MessageBody=json.dumps(cloudwatch_event()))
        sqs.send_message(QueueUrl=url, MessageBody=json.dumps(alertmanager()))
        sqs.send_message(QueueUrl=url, MessageBody="garbage")

        intake = SqsIntake(url, lambda: service, client=sqs)
        assert intake.poll_once(wait_seconds=0) == 2

        assert {i["service"] for i in service.list_incidents()} == {"checkout-api", "auth-service"}
        attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessagesNotVisible"])
        assert attrs["Attributes"]["ApproximateNumberOfMessagesNotVisible"] == "1"  # the garbage, not deleted
