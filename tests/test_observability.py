"""Operational metrics from the audit trail, and OpenTelemetry instruments."""

from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from opsrelay import ops_metrics, telemetry


def test_metrics_from_the_audit_trail(service):
    service.simulate("bad-deploy")
    [approval] = service.list_approvals()
    service.decide_approval(approval["id"], approve=True, approver="jane")
    service.simulate("traffic-spike")  # allowed by policy: no human acknowledgement to measure
    service.open_incident("Users say the site feels slow", "No alert fired.")  # escalated

    m = ops_metrics.compute(service.store)

    assert m["incidents"]["by_status"] == {"resolved": 2, "escalated": 1}
    assert (m["incidents"]["open"], m["incidents"]["resolved_rate"]) == (0, 0.667)
    stages = m["stages"]
    assert stages["time_to_resolve"]["count"] == 2
    assert stages["time_to_acknowledge"]["count"] == 1  # only the person's decision
    assert stages["time_to_diagnose"]["count"] == 2 and stages["time_to_verify"]["count"] == 2
    assert m["agents"]["triage"]["calls"] == 3 and m["agents"]["verification"]["calls"] == 2
    assert m["policy_decisions"] == {"APPROVAL_REQUIRED": 1, "ALLOW": 1}


def test_percentiles():
    assert ops_metrics.summarize([]) == {"count": 0, "median": None, "p90": None}
    assert ops_metrics.summarize([4, 1, 3, 2, 10]) == {"count": 5, "median": 3, "p90": 10}


def test_detection_lag_comes_from_the_alert(service):
    alarm = {
        "detail-type": "CloudWatch Alarm State Change",
        "resources": ["arn:aws:cloudwatch:us-east-1:1:alarm:checkout-api-5xx"],
        "detail": {"alarmName": "checkout-api-5xx", "state": {"value": "ALARM", "timestamp": "2026-01-01T00:00:00Z"}},
    }
    service.ingest(alarm)
    assert ops_metrics.compute(service.store)["stages"]["time_to_detect"]["count"] == 1


def _points(reader):
    names = {}
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                names[metric.name] = [dict(p.attributes) for p in metric.data.data_points]
    return names


def test_opentelemetry_instruments():
    reader = InMemoryMetricReader()
    telemetry.configure(reader=reader)
    try:
        from opsrelay.environment import SimulatedEnvironment
        from opsrelay.service import IncidentService
        from opsrelay.store.sqlite import SqliteStore

        store = SqliteStore(":memory:")
        env = SimulatedEnvironment(store)
        env.seed()
        svc = IncidentService(store=store, env=env)
        svc.simulate("traffic-spike")

        points = _points(reader)
        assert {"source": "alertmanager"} in points["opsrelay_incidents_total"]
        assert {"outcome": "resolved"} in points["opsrelay_incidents_closed_total"]
        assert {"decision": "ALLOW"} in points["opsrelay_policy_decisions_total"]
        assert {"outcome": "ok"} in points["opsrelay_executions_total"]
        assert {"agent": "triage", "outcome": "ok"} in points["opsrelay_agent_calls_total"]
        assert {"agent": "triage"} in points["opsrelay_agent_latency_seconds"]
        stages = {p["stage"] for p in points["opsrelay_stage_duration_seconds"]}
        assert {"open", "triaging", "investigating", "remediating", "verifying"} <= stages
    finally:
        telemetry.reset()
