"""The production environment: real services on AWS instead of the simulation.

    OPSRELAY_ENVIRONMENT=aws
    OPSRELAY_SERVICE_CATALOG=/path/to/catalog.yaml    (see deploy/catalog.example.yaml)

Observation comes from the service catalog (tiers, owners, dependencies), CloudWatch metrics and
CloudWatch Logs; deployments and actions from Amazon ECS. A service without an `ecs` entry can be
observed but not changed: actions on it are refused, never guessed. Scenarios (`simulate`) exist
only in the simulated environment.
"""

from typing import Any

from ..environment import ACTIONS, Environment
from ..store import Record
from .catalog import ServiceCatalog
from .cloudwatch import CloudWatchLogs, CloudWatchMetrics
from .ecs import EcsDeployer


class HybridEnvironment(Environment):
    """Real services from the catalog, the rest simulated: OPSRELAY_ENVIRONMENT=hybrid.

    Lets one deployment handle a real workload (CloudWatch, ECS) while the demo scenarios keep
    working for the simulated services. Each call goes to whichever side owns the service."""

    def __init__(self, simulated: Environment, real: "AwsEnvironment"):
        self.sim = simulated
        self.real = real

    def _side(self, service: str) -> Environment:
        return self.real if service in self.real.catalog.entries else self.sim

    def seed(self, reset: bool = False) -> None:
        self.sim.seed(reset)

    def inject(self, scenario: str) -> Record:
        return self.sim.inject(scenario)

    def health_overview(self) -> list[Record]:
        real = self.real.health_overview()
        names = {s["service"] for s in real}  # a real service shadows a simulated one of the same name
        return [*real, *(s for s in self.sim.health_overview() if s["service"] not in names)]

    def service_info(self, service: str) -> Record:
        return self._side(service).service_info(service)

    def metrics(self, service: str) -> Record:
        return self._side(service).metrics(service)

    def logs(self, service: str, query: str = "") -> list[str]:
        return self._side(service).logs(service, query)

    def deployments(self, service: str) -> list[Record]:
        return self._side(service).deployments(service)

    def execute(self, action: str, service: str, params: Record, idempotency_key: str | None = None) -> Record:
        return self._side(service).execute(action, service, params, idempotency_key)

    def reconcile(self, action: str, service: str, params: Record, idempotency_key: str) -> str:
        return self._side(service).reconcile(action, service, params, idempotency_key)


class AwsEnvironment(Environment):
    def __init__(
        self,
        catalog: ServiceCatalog,
        metrics: CloudWatchMetrics,
        logs: CloudWatchLogs,
        deployer: EcsDeployer | None = None,
    ):
        self.catalog = catalog
        self.cw = metrics
        self.cw_logs = logs
        self.deployer = deployer

    @classmethod
    def from_settings(cls, catalog_path: str, region: str, **clients: Any) -> "AwsEnvironment":
        if not catalog_path:
            raise ValueError("OPSRELAY_ENVIRONMENT=aws needs OPSRELAY_SERVICE_CATALOG (a YAML service catalog)")
        return cls(
            ServiceCatalog.load(catalog_path),
            CloudWatchMetrics(clients.get("cloudwatch"), region),
            CloudWatchLogs(clients.get("logs"), region),
            EcsDeployer(clients.get("ecs"), region),
        )

    def _deployable(self, name: str) -> bool:
        entry = self.catalog.get(name)
        return self.deployer is not None and bool(entry.ecs)

    # Observation
    def health_overview(self) -> list[Record]:
        out = []
        for name in self.catalog.names():
            entry = self.catalog.get(name)
            m = self.cw.metrics(entry)
            out.append(
                {
                    "service": name,
                    "tier": entry.tier,
                    "healthy": m["healthy"],
                    "error_rate": m["error_rate"] if m["error_rate"] is not None else 0.0,
                    "p99_latency_ms": m["p99_latency_ms"] if m["p99_latency_ms"] is not None else 0,
                }
            )
        return out

    def service_info(self, service: str) -> Record:
        entry = self.catalog.get(service)
        info = {
            "name": entry.name,
            "description": entry.description,
            "tier": entry.tier,
            "owner_team": entry.owner_team,
            "depends_on": list(entry.depends_on),
            "max_replicas": entry.max_replicas,
            "version": None,
            "replicas": None,
        }
        if self._deployable(service):
            info["version"] = self.deployer.current_version(entry)
            info["replicas"] = self.deployer.replicas(entry)
        return info

    def metrics(self, service: str) -> Record:
        entry = self.catalog.get(service)
        m = self.cw.metrics(entry)
        if self._deployable(service):
            m["replicas"] = self.deployer.replicas(entry)
        return m

    def logs(self, service: str, query: str = "") -> list[str]:
        return self.cw_logs.logs(self.catalog.get(service), query)

    def deployments(self, service: str) -> list[Record]:
        if not self._deployable(service):
            return []
        return self.deployer.deployments(self.catalog.get(service))

    # Action
    def execute(self, action: str, service: str, params: Record, idempotency_key: str | None = None) -> Record:
        if action not in ACTIONS:
            return {"ok": False, "detail": f"Unknown action {action}"}
        if not self._deployable(service):
            return {"ok": False, "detail": f"{service} has no deployment connector; make this change manually"}
        return self.deployer.execute(self.catalog.get(service), action, params, idempotency_key)

    def reconcile(self, action: str, service: str, params: Record, idempotency_key: str) -> str:
        if not self._deployable(service):
            return "unknown"
        return self.deployer.reconcile(self.catalog.get(service), idempotency_key)
