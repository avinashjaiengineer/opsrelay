"""The IT environment agents observe and act on.

`Environment` is the connector interface. `SimulatedEnvironment` implements it over the store,
so the whole workflow runs end to end without real infrastructure: scenarios inject a fault
into a service, the fault shows up in metrics and logs, and the right remediation clears it.
To connect real systems, implement `Environment` against CloudWatch, your CMDB, your deploy
tool, and so on, and return it from `get_environment()`.
"""

from abc import ABC, abstractmethod
from typing import Any

from .store import Record, Store, now_iso

ACTIONS: dict[str, dict[str, Any]] = {
    "rollback_deployment": {
        "risk": "medium",
        "description": "Roll the service back to its previous version.",
        "params": {},
    },
    "restart_service": {
        "risk": "medium",
        "description": "Rolling restart of every replica of the service.",
        "params": {},
    },
    "scale_service": {
        "risk": "low",
        "description": 'Set the replica count. Params: {"replicas": <int>}.',
        "params": {"replicas": "int"},
    },
    "flush_cache": {
        "risk": "low",
        "description": "Flush the service's cache.",
        "params": {},
    },
}
RISK_ORDER = ["low", "medium", "high"]

RUNBOOKS: dict[str, str] = {
    "bad-deploy": (
        "Runbook: error spike after a deployment\n"
        "1. Confirm the error rate rose after the latest deploy (compare timestamps).\n"
        "2. Check logs for new exception types introduced by the release.\n"
        "3. Remediate: rollback_deployment to the previous version.\n"
        "4. Verify error rate is back under 1% within 5 minutes; open a bug for the release owner."
    ),
    "memory-leak": (
        "Runbook: memory exhaustion / OOMKilled\n"
        "1. Confirm memory is near the limit and pods are being OOMKilled.\n"
        "2. Check whether a recent deploy changed memory behaviour; if so prefer rollback.\n"
        "3. Remediate: restart_service to reclaim memory (buys time, not a fix).\n"
        "4. Verify memory and latency recover; file a follow-up to find the leak."
    ),
    "saturation": (
        "Runbook: CPU saturation / traffic spike\n"
        "1. Confirm CPU is above 85% across replicas and request rate is above baseline.\n"
        "2. Rule out a bad deploy (no recent release) and a retry storm from a dependency.\n"
        "3. Remediate: scale_service to about 2x current replicas, within max_replicas.\n"
        "4. Verify CPU below 70% and p99 latency back to baseline."
    ),
}

SEED_SERVICES: list[Record] = [
    {
        "name": "web-frontend",
        "description": "Customer-facing web app",
        "tier": 1,
        "owner_team": "web",
        "depends_on": ["checkout-api", "auth-service"],
        "version": "5.2.1",
        "replicas": 6,
        "max_replicas": 20,
    },
    {
        "name": "checkout-api",
        "description": "Cart, pricing and order placement",
        "tier": 1,
        "owner_team": "commerce",
        "depends_on": ["payments-db", "inventory-service", "redis-cache"],
        "version": "2.13.4",
        "replicas": 4,
        "max_replicas": 12,
    },
    {
        "name": "auth-service",
        "description": "Login, sessions and tokens",
        "tier": 1,
        "owner_team": "identity",
        "depends_on": ["redis-cache"],
        "version": "3.8.0",
        "replicas": 3,
        "max_replicas": 10,
    },
    {
        "name": "inventory-service",
        "description": "Stock levels and reservations",
        "tier": 2,
        "owner_team": "commerce",
        "depends_on": ["redis-cache"],
        "version": "1.22.0",
        "replicas": 2,
        "max_replicas": 8,
    },
    {
        "name": "payments-db",
        "description": "PostgreSQL cluster for payments",
        "tier": 1,
        "owner_team": "data",
        "depends_on": [],
        "version": "16.4",
        "replicas": 2,
        "max_replicas": 2,
    },
    {
        "name": "redis-cache",
        "description": "Shared Redis cache",
        "tier": 2,
        "owner_team": "platform",
        "depends_on": [],
        "version": "7.2",
        "replicas": 3,
        "max_replicas": 6,
    },
]

SCENARIOS: dict[str, Record] = {
    "bad-deploy": {
        "service": "checkout-api",
        "fault": {"type": "bad_deploy"},
        "new_version": "2.14.0",
        "alert": {
            "title": "checkout-api 5xx error rate above 20%",
            "description": "Alertmanager: HighErrorRate firing for checkout-api (error_rate=0.23, threshold 0.05). "
            "Customers report failed checkouts.",
        },
    },
    "memory-leak": {
        "service": "auth-service",
        "fault": {"type": "memory_leak"},
        "alert": {
            "title": "auth-service pods OOMKilled, login latency high",
            "description": "Alertmanager: KubePodOOMKilled for auth-service (3 restarts in 20m); "
            "p99 login latency 4.1s.",
        },
    },
    "traffic-spike": {
        "service": "inventory-service",
        "fault": {"type": "saturation", "needed_replicas": 4},
        "alert": {
            "title": "inventory-service p99 latency 2.8s",
            "description": "Alertmanager: HighLatency firing for inventory-service (p99=2.8s, threshold 0.5s). "
            "Flash sale started 10 minutes ago.",
        },
    },
}


class Environment(ABC):
    @abstractmethod
    def health_overview(self) -> list[Record]: ...

    @abstractmethod
    def service_info(self, service: str) -> Record: ...

    @abstractmethod
    def metrics(self, service: str) -> Record: ...

    @abstractmethod
    def logs(self, service: str, query: str = "") -> list[str]: ...

    @abstractmethod
    def deployments(self, service: str) -> list[Record]: ...

    @abstractmethod
    def runbook(self, topic: str) -> str: ...

    @abstractmethod
    def execute(self, action: str, service: str, params: Record) -> Record:
        """Carry out an approved action. Returns {"ok": bool, "detail": str}."""

    def action_risk(self, action: str, service: str) -> str:
        """Base risk of the action, one level higher on tier-1 services."""
        base = ACTIONS[action]["risk"]
        info = self.service_info(service)
        if info.get("tier") == 1:
            return RISK_ORDER[min(RISK_ORDER.index(base) + 1, len(RISK_ORDER) - 1)]
        return base


class SimulatedEnvironment(Environment):
    def __init__(self, store: Store):
        self.store = store

    # Setup
    def seed(self, reset: bool = False) -> None:
        for spec in SEED_SERVICES:
            if reset or self.store.get_service(spec["name"]) is None:
                self.store.put_service(
                    {
                        **spec,
                        "fault": None,
                        "deployments": [
                            {"version": spec["version"], "at": "2026-09-01T10:00:00+00:00", "by": "ci"},
                        ],
                    }
                )

    def inject(self, scenario: str) -> Record:
        """Start a scenario and return the alert it raises."""
        spec = SCENARIOS[scenario]
        self.seed()
        svc = self._svc(spec["service"])
        if spec["fault"]["type"] == "bad_deploy":
            svc["previous_version"] = svc["version"]
            svc["version"] = spec["new_version"]
            svc["deployments"].append({"version": spec["new_version"], "at": now_iso(), "by": "ci"})
        svc["fault"] = {**spec["fault"], "since": now_iso()}
        self.store.put_service(svc)
        return {"service": spec["service"], **spec["alert"]}

    def _svc(self, name: str) -> Record:
        svc = self.store.get_service(name)
        if svc is None:
            known = ", ".join(s["name"] for s in self.store.list_services())
            raise KeyError(f"Unknown service '{name}'. Known services: {known}")
        return svc

    # Observation
    def health_overview(self) -> list[Record]:
        out = []
        for svc in self.store.list_services():
            m = self._metrics(svc)
            out.append(
                {
                    "service": svc["name"],
                    "tier": svc["tier"],
                    "healthy": m["healthy"],
                    "error_rate": m["error_rate"],
                    "p99_latency_ms": m["p99_latency_ms"],
                }
            )
        return out

    def service_info(self, service: str) -> Record:
        svc = self._svc(service)
        return {
            k: svc[k]
            for k in ("name", "description", "tier", "owner_team", "depends_on", "version", "replicas", "max_replicas")
        }

    def metrics(self, service: str) -> Record:
        return self._metrics(self._svc(service))

    @staticmethod
    def _metrics(svc: Record) -> Record:
        m = {
            "service": svc["name"],
            "error_rate": 0.002,
            "p99_latency_ms": 180,
            "cpu_pct": 35,
            "memory_pct": 48,
            "requests_per_min": 1200,
            "pod_restarts_30m": 0,
            "replicas": svc["replicas"],
        }
        fault = svc.get("fault")
        kind = fault and fault["type"]
        if kind == "bad_deploy":
            m.update(error_rate=0.23, p99_latency_ms=950)
        elif kind == "memory_leak":
            m.update(memory_pct=97, pod_restarts_30m=3, p99_latency_ms=4100, error_rate=0.04)
        elif kind == "saturation":
            m.update(cpu_pct=96, p99_latency_ms=2800, requests_per_min=5200, error_rate=0.03)
        m["healthy"] = m["error_rate"] < 0.01 and m["p99_latency_ms"] < 500
        return m

    def logs(self, service: str, query: str = "") -> list[str]:
        svc = self._svc(service)
        fault = svc.get("fault")
        kind = fault and fault["type"]
        lines = [f"INFO  {service} request completed status=200 duration_ms=42"] * 2
        if kind == "bad_deploy":
            lines = [
                f"ERROR {service} v{svc['version']} NullPointerException in "
                "PriceCalculator.applyPromotion(PriceCalculator.java:88)",
                f"ERROR {service} v{svc['version']} POST /checkout status=500 duration_ms=31",
                f"WARN  {service} v{svc['version']} promotion rules cache miss for key 'promo:v2'",
            ] * 3
        elif kind == "memory_leak":
            lines = [
                f"WARN  {service} heap usage 95% after full GC",
                f"ERROR {service} pod {service}-7d9f OOMKilled (limit 2Gi)",
                f"WARN  {service} session cache size 1.8M entries (expected < 200k)",
            ] * 3
        elif kind == "saturation":
            lines = [
                f"WARN  {service} request queue depth 480 (threshold 100)",
                f"WARN  {service} upstream timeout calling redis-cache after 2000ms",
                f"INFO  {service} traffic 4.3x baseline (campaign=flash-sale)",
            ] * 3
        if query:
            q = query.lower()
            matched = [line for line in lines if q in line.lower()]
            lines = matched or lines
        return lines[:20]

    def deployments(self, service: str) -> list[Record]:
        return self._svc(service)["deployments"][-5:]

    def runbook(self, topic: str) -> str:
        t = topic.lower()
        for key, text in RUNBOOKS.items():
            words = key.replace("-", " ").split()
            if key in t or any(w in t for w in words) or t in text.lower():
                return text
        return "No matching runbook. Available: " + ", ".join(RUNBOOKS)

    # Action
    def execute(self, action: str, service: str, params: Record) -> Record:
        if action not in ACTIONS:
            return {"ok": False, "detail": f"Unknown action {action}"}
        svc = self._svc(service)
        fault = svc.get("fault") or {}
        kind = fault.get("type")
        detail: str
        if action == "rollback_deployment":
            prev = svc.get("previous_version")
            if not prev:
                return {"ok": False, "detail": f"{service} has no previous version to roll back to"}
            svc["version"], svc["previous_version"] = prev, None
            svc["deployments"].append({"version": prev, "at": now_iso(), "by": "agentmesh-rollback"})
            if kind == "bad_deploy":
                svc["fault"] = None
            detail = f"Rolled {service} back to {prev}"
        elif action == "restart_service":
            if kind == "memory_leak":
                svc["fault"] = None
            detail = f"Rolling restart of {svc['replicas']} replicas of {service} completed"
        elif action == "scale_service":
            replicas = int(params.get("replicas", 0))
            if not 1 <= replicas <= svc["max_replicas"]:
                return {"ok": False, "detail": f"replicas must be between 1 and {svc['max_replicas']}"}
            svc["replicas"] = replicas
            if kind == "saturation" and replicas >= fault.get("needed_replicas", 0):
                svc["fault"] = None
            detail = f"Scaled {service} to {replicas} replicas"
        else:  # flush_cache
            detail = f"Flushed cache for {service}"
        self.store.put_service(svc)
        return {"ok": True, "detail": detail}


def get_environment(store: Store) -> Environment:
    env = SimulatedEnvironment(store)
    env.seed()
    return env
