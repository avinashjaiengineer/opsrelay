"""The service catalog: what OpsRelay knows about each service, from a YAML file.

    services:
      checkout-api:
        description: Checkout and payment orchestration
        tier: 1                       # 1 = most critical; raises remediation risk
        owner_team: payments
        depends_on: [payments-db]
        max_replicas: 10
        log_group: /ecs/checkout-api
        ecs: {cluster: prod, service: checkout-api}
        healthy_when: {error_rate_below: 0.01, p99_latency_ms_below: 500}   # also cpu_pct_below, memory_pct_below
        metrics:                      # CloudWatch metric queries (see cloudwatch.py)
          errors:   {namespace: AWS/ApplicationELB, name: HTTPCode_Target_5XX_Count, stat: Sum, dimensions: {...}}
          requests: {namespace: AWS/ApplicationELB, name: RequestCount, stat: Sum, dimensions: {...}}
          p99_latency_seconds: {namespace: AWS/ApplicationELB, name: TargetResponseTime, stat: p99, dimensions: {...}}
          cpu_pct:    {namespace: AWS/ECS, name: CPUUtilization, stat: Average, dimensions: {...}}
          memory_pct: {namespace: AWS/ECS, name: MemoryUtilization, stat: Average, dimensions: {...}}
        window_minutes: 5             # how far back metrics look (shorter reacts faster)
        settle_seconds: 0             # after an ECS change: wait for the rollout, then this long, so
                                      # verification judges metrics from the new tasks

See deploy/catalog.example.yaml.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ServiceEntry:
    name: str
    description: str = ""
    tier: int = 3
    owner_team: str = ""
    depends_on: tuple[str, ...] = ()
    max_replicas: int = 1
    log_group: str | None = None
    ecs: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    error_rate_below: float = 0.01
    p99_latency_ms_below: float = 500
    cpu_pct_below: float | None = None
    memory_pct_below: float | None = None
    window_minutes: int = 5
    settle_seconds: int = 0


class ServiceCatalog:
    def __init__(self, entries: dict[str, ServiceEntry]):
        self.entries = entries

    @classmethod
    def load(cls, path: str) -> "ServiceCatalog":
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.from_dict(doc)

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> "ServiceCatalog":
        entries = {}
        for name, spec in (doc.get("services") or {}).items():
            healthy = spec.get("healthy_when") or {}
            entries[name] = ServiceEntry(
                name=name,
                description=spec.get("description", ""),
                tier=int(spec.get("tier", 3)),
                owner_team=spec.get("owner_team", ""),
                depends_on=tuple(spec.get("depends_on") or ()),
                max_replicas=int(spec.get("max_replicas", 1)),
                log_group=spec.get("log_group"),
                ecs=dict(spec.get("ecs") or {}),
                metrics=dict(spec.get("metrics") or {}),
                error_rate_below=float(healthy.get("error_rate_below", 0.01)),
                p99_latency_ms_below=float(healthy.get("p99_latency_ms_below", 500)),
                cpu_pct_below=healthy.get("cpu_pct_below"),
                memory_pct_below=healthy.get("memory_pct_below"),
                window_minutes=int(spec.get("window_minutes", 5)),
                settle_seconds=int(spec.get("settle_seconds", 0)),
            )
        if not entries:
            raise ValueError("the service catalog lists no services")
        return cls(entries)

    def get(self, name: str) -> ServiceEntry:
        if name not in self.entries:
            raise KeyError(f"Unknown service '{name}'. Known services: {', '.join(sorted(self.entries))}")
        return self.entries[name]

    def names(self) -> list[str]:
        return sorted(self.entries)
