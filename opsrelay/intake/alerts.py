"""One alert model for every source, and the parsers that produce it.

    CloudWatch alarm state change (EventBridge event, or the SNS message of a classic alarm)
    Prometheus Alertmanager webhook (version 4 payload: one or more alerts)
    OpsRelay API (open_incident)
                              |
                              v
                   Alert(source, fingerprint, status, service, ...)

The fingerprint identifies "the same problem" across repeats: the CloudWatch alarm ARN (or name),
or Alertmanager's own fingerprint (derived from the alert's labels).
"""

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

AlertStatus = Literal["firing", "resolved"]


class Alert(BaseModel):
    source: str  # cloudwatch | alertmanager | api
    fingerprint: str
    status: AlertStatus
    title: str
    description: str = ""
    service: str | None = None
    severity: str | None = None  # the source's own severity label, if any
    labels: dict[str, str] = Field(default_factory=dict)
    started_at: str | None = None
    url: str | None = None


class UnrecognizedAlert(ValueError):
    pass


def _fp(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# CloudWatch ---------------------------------------------------------------------------------------

SERVICE_DIMENSIONS = ("ServiceName", "service", "Service", "FunctionName", "DBInstanceIdentifier")


def _service_from_dimensions(dimensions: dict[str, str], known: list[str], alarm_name: str) -> str | None:
    for key in SERVICE_DIMENSIONS:
        if dimensions.get(key) in known:
            return dimensions[key]
    lowered = alarm_name.lower()
    return next((s for s in sorted(known, key=len, reverse=True) if s.lower() in lowered), None)


def from_cloudwatch_event(event: dict[str, Any], known_services: list[str]) -> Alert:
    """An EventBridge "CloudWatch Alarm State Change" event."""
    detail = event.get("detail") or {}
    name = detail.get("alarmName")
    state = (detail.get("state") or {}).get("value")
    if not name or state not in ("ALARM", "OK", "INSUFFICIENT_DATA"):
        raise UnrecognizedAlert("not a CloudWatch alarm state change")
    dimensions: dict[str, str] = {}
    for metric in (detail.get("configuration") or {}).get("metrics") or []:
        dims = ((metric.get("metricStat") or {}).get("metric") or {}).get("dimensions") or {}
        dimensions.update({k: str(v) for k, v in dims.items()})
    reason = (detail.get("state") or {}).get("reason", "")
    arn = (event.get("resources") or [None])[0] or f"alarm:{name}"
    return Alert(
        source="cloudwatch",
        fingerprint=_fp("cloudwatch", arn),
        status="firing" if state == "ALARM" else "resolved",
        title=f"CloudWatch alarm {name}",
        description=(detail.get("configuration") or {}).get("description") or reason,
        service=_service_from_dimensions(dimensions, known_services, name),
        labels={"alarm": name, **dimensions},
        started_at=(detail.get("state") or {}).get("timestamp") or event.get("time"),
        url=None,
    )


def from_cloudwatch_sns(message: dict[str, Any], known_services: list[str]) -> Alert:
    """The SNS notification of a classic CloudWatch alarm action."""
    name = message.get("AlarmName")
    state = message.get("NewStateValue")
    if not name or not state:
        raise UnrecognizedAlert("not a CloudWatch alarm notification")
    trigger = message.get("Trigger") or {}
    dimensions = {
        d.get("name") or d.get("Name"): str(d.get("value") or d.get("Value")) for d in trigger.get("Dimensions") or []
    }
    arn = message.get("AlarmArn") or f"alarm:{name}"
    return Alert(
        source="cloudwatch",
        fingerprint=_fp("cloudwatch", arn),
        status="firing" if state == "ALARM" else "resolved",
        title=f"CloudWatch alarm {name}",
        description=message.get("AlarmDescription") or message.get("NewStateReason", ""),
        service=_service_from_dimensions(dimensions, known_services, name),
        labels={"alarm": name, **dimensions},
        started_at=message.get("StateChangeTime"),
    )


# Alertmanager -------------------------------------------------------------------------------------


def from_alertmanager(payload: dict[str, Any], known_services: list[str]) -> list[Alert]:
    """A Prometheus Alertmanager webhook (version 4): one Alert per alert in the group."""
    if "alerts" not in payload:
        raise UnrecognizedAlert("not an Alertmanager webhook payload")
    alerts = []
    for a in payload["alerts"]:
        labels = {k: str(v) for k, v in (a.get("labels") or {}).items()}
        annotations = a.get("annotations") or {}
        name = labels.get("alertname", "alert")
        service = next(
            (
                labels[k]
                for k in ("service", "app", "job", "container", "deployment")
                if labels.get(k) in known_services
            ),
            None,
        )
        fingerprint = a.get("fingerprint") or _fp(*(f"{k}={v}" for k, v in sorted(labels.items())))
        alerts.append(
            Alert(
                source="alertmanager",
                fingerprint=_fp("alertmanager", fingerprint),
                status="resolved" if a.get("status") == "resolved" else "firing",
                title=annotations.get("summary") or f"{name}" + (f" on {service}" if service else ""),
                description=annotations.get("description") or annotations.get("message") or "",
                service=service,
                severity=labels.get("severity"),
                labels=labels,
                started_at=a.get("startsAt"),
                url=a.get("generatorURL"),
            )
        )
    return alerts


# Any supported message ----------------------------------------------------------------------------


def parse(message: Any, known_services: list[str]) -> list[Alert]:
    """Recognize a message from SQS or a webhook: an EventBridge event, an SNS envelope (of a
    CloudWatch alarm or an EventBridge event), or an Alertmanager payload."""
    if isinstance(message, str):
        try:
            message = json.loads(message)
        except ValueError as e:
            raise UnrecognizedAlert("message is not JSON") from e
    if not isinstance(message, dict):
        raise UnrecognizedAlert("message is not a JSON object")
    if message.get("Type") == "Notification" and "Message" in message:  # SNS envelope
        inner = message["Message"]
        inner = json.loads(inner) if isinstance(inner, str) else inner
        if isinstance(inner, dict) and "AlarmName" in inner:
            return [from_cloudwatch_sns(inner, known_services)]
        return parse(inner, known_services)
    if message.get("detail-type") == "CloudWatch Alarm State Change":
        return [from_cloudwatch_event(message, known_services)]
    if "alerts" in message and isinstance(message["alerts"], list):
        return from_alertmanager(message, known_services)
    if "AlarmName" in message:
        return [from_cloudwatch_sns(message, known_services)]
    raise UnrecognizedAlert("unrecognized alert format")
