"""Alert intake: deduplication, correlation, and incident creation.

For each alert:

    firing, fingerprint of an open incident   -> duplicate: counted on that incident
    firing, same service as an open incident
      opened within the correlation window    -> correlated: attached to that incident
    firing, otherwise                         -> a new incident, its coordination queued as a job
    resolved                                  -> noted on the incident (verification still decides)

Deduplication is race-free: an "alert_key" record per fingerprint is claimed (insert-if-absent, or
compare-and-set once its incident is closed) *before* the incident is created, with the incident's
id chosen up front. Two copies of an alert arriving together can't both open incidents.
"""

from datetime import UTC, datetime, timedelta

from .. import jobs, telemetry
from ..config import get_settings
from ..lifecycle import TERMINAL
from ..store import Record, new_incident_id, now_iso
from ..store.base import make_event, new_record
from .alerts import Alert

KEY = "alert_key"
IN_FLIGHT_SECONDS = 60


def _age_seconds(iso: str) -> float:
    return (datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds()


def _open(incident: Record | None) -> bool:
    return incident is not None and incident["status"] not in TERMINAL


def _attach(svc, incident: Record, alert: Alert, kind: str, message: str) -> None:  # noqa: ANN001
    fingerprints = incident.get("fingerprints") or []
    svc.store.update_incident(
        incident["id"],
        alerts_count=int(incident.get("alerts_count") or 1) + 1,
        last_alert_at=now_iso(),
        fingerprints=fingerprints if alert.fingerprint in fingerprints else [*fingerprints, alert.fingerprint],
        events=[
            make_event(
                incident["id"],
                f"source:{alert.source}",
                kind,
                message,
                {"fingerprint": alert.fingerprint, "labels": alert.labels, "title": alert.title},
            )
        ],
    )


def _correlate(svc, alert: Alert) -> Record | None:  # noqa: ANN001
    if not alert.service:
        return None
    window = timedelta(minutes=get_settings().correlation_window_minutes)
    cutoff = (datetime.now(UTC) - window).isoformat(timespec="milliseconds")
    for incident in svc.store.list_incidents(limit=200):
        if _open(incident) and incident.get("service") == alert.service and incident["created_at"] >= cutoff:
            return incident
    return None


def ingest(svc, alert: Alert, *, queue: bool = True) -> Record:  # noqa: ANN001
    """Route one alert. Returns {"outcome", "incident_id", ...}."""
    with telemetry.span("opsrelay.ingest", source=alert.source, fingerprint=alert.fingerprint):
        result = _route(svc, alert, queue)
    telemetry.count("alerts_total", source=alert.source, outcome=result["outcome"])
    return result


def _route(svc, alert: Alert, queue: bool) -> Record:  # noqa: ANN001
    store = svc.store
    for _ in range(5):
        key = store.get_record(KEY, alert.fingerprint)
        incident = store.get_incident(key["incident_id"]) if key else None

        if alert.status == "resolved":
            if not _open(incident):
                return {"outcome": "ignored", "incident_id": key and key["incident_id"], "reason": "no open incident"}
            svc.store.update_incident(
                incident["id"],
                source_resolved_at=now_iso(),
                events=[
                    make_event(
                        incident["id"],
                        f"source:{alert.source}",
                        "alert.resolved",
                        f"{alert.title}: the source reports it resolved (verification still decides)",
                        {"fingerprint": alert.fingerprint},
                    )
                ],
            )
            return {"outcome": "resolved_noted", "incident_id": incident["id"]}

        if _open(incident):
            _attach(svc, incident, alert, "alert.duplicate", f"Repeat of {alert.title}")
            return {"outcome": "deduplicated", "incident_id": incident["id"]}
        if key is not None and incident is None and _age_seconds(key["updated_at"]) < IN_FLIGHT_SECONDS:
            # Another copy of this alert claimed the fingerprint and is creating the incident now.
            return {"outcome": "deduplicated", "incident_id": key["incident_id"], "note": "incident being created"}

        related = _correlate(svc, alert)
        if related is not None:
            if key is None:
                claimed = store.put_record(new_record(KEY, alert.fingerprint, "open", incident_id=related["id"]))
            else:
                claimed = (
                    store.move_record(KEY, alert.fingerprint, key["rev"], {"incident_id": related["id"]}) is not None
                )
            if not claimed:
                continue  # someone else just claimed this fingerprint: look again
            _attach(svc, related, alert, "alert.correlated", f"Correlated: {alert.title} (same service, open incident)")
            return {"outcome": "correlated", "incident_id": related["id"]}

        planned = new_incident_id()
        if key is None:
            claimed = store.put_record(new_record(KEY, alert.fingerprint, "open", incident_id=planned))
        else:
            claimed = store.move_record(KEY, alert.fingerprint, key["rev"], {"incident_id": planned}) is not None
        if not claimed:
            continue
        opened = svc.open_incident(
            alert.title,
            alert.description,
            source=alert.source,
            service=alert.service,
            external_ref=alert.fingerprint,
            run=False,
            incident_id=planned,
            alert=alert.model_dump(),
        )
        result = {"outcome": "created", "incident_id": planned}
        if queue:
            from ..service import new_incident_prompt

            result["job_id"] = jobs.enqueue(store, planned, new_incident_prompt(opened["incident"]))["id"]
        return result
    raise RuntimeError(f"Could not route alert {alert.fingerprint}: it kept changing concurrently")


def known_services(svc) -> list[str]:  # noqa: ANN001
    return [s["service"] for s in svc.env.health_overview()]
