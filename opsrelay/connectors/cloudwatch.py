"""Metrics and logs from Amazon CloudWatch.

Each service's catalog entry names the metrics that describe it (see catalog.py). Supported keys:

    errors + requests       -> error_rate = errors / requests over the window
    error_rate              -> used as-is (0.0 - 1.0)
    p99_latency_seconds     -> p99_latency_ms (x1000)   or p99_latency_ms as-is
    cpu_pct, memory_pct, requests_per_min

Missing metrics are reported as None rather than guessed. A service is healthy only if it has data
and every threshold in its `healthy_when` that applies to that data is met.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .catalog import ServiceEntry

WINDOW_MINUTES = 5


class CloudWatchMetrics:
    def __init__(self, client: Any = None, region: str | None = None):
        if client is None:
            import boto3

            client = boto3.client("cloudwatch", region_name=region)
        self.client = client

    def metrics(self, entry: ServiceEntry) -> dict[str, Any]:
        window = entry.window_minutes or WINDOW_MINUTES
        now = datetime.now(UTC)
        start = now - timedelta(minutes=window)
        end = now + timedelta(minutes=1)  # include the newest data points, which arrive late
        queries = []
        for key, spec in entry.metrics.items():
            queries.append(
                {
                    "Id": key.lower(),
                    "MetricStat": {
                        "Metric": {
                            "Namespace": spec["namespace"],
                            "MetricName": spec["name"],
                            "Dimensions": [
                                {"Name": k, "Value": str(v)} for k, v in (spec.get("dimensions") or {}).items()
                            ],
                        },
                        "Period": window * 60,
                        "Stat": spec.get("stat", "Average"),
                    },
                    "ReturnData": True,
                }
            )
        values: dict[str, float | None] = {}
        if queries:
            resp = self.client.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
            for result in resp.get("MetricDataResults", []):
                values[result["Id"]] = result["Values"][0] if result.get("Values") else None

        def v(key: str) -> float | None:
            return values.get(key)

        error_rate = v("error_rate")
        if error_rate is None and v("errors") is not None and v("requests"):
            error_rate = v("errors") / v("requests")
        elif error_rate is None and v("errors") is not None and v("requests") == 0:
            error_rate = 0.0
        p99 = v("p99_latency_ms")
        if p99 is None and v("p99_latency_seconds") is not None:
            p99 = v("p99_latency_seconds") * 1000
        requests_per_min = v("requests_per_min")
        if requests_per_min is None and v("requests") is not None:
            requests_per_min = v("requests") / window

        m = {
            "service": entry.name,
            "error_rate": round(error_rate, 4) if error_rate is not None else None,
            "p99_latency_ms": round(p99) if p99 is not None else None,
            "cpu_pct": round(v("cpu_pct")) if v("cpu_pct") is not None else None,
            "memory_pct": round(v("memory_pct")) if v("memory_pct") is not None else None,
            "requests_per_min": round(requests_per_min) if requests_per_min is not None else None,
            "window_minutes": window,
            "source": "cloudwatch",
        }
        checks = [
            (m["error_rate"], entry.error_rate_below),
            (m["p99_latency_ms"], entry.p99_latency_ms_below),
            (m["cpu_pct"], entry.cpu_pct_below),
            (m["memory_pct"], entry.memory_pct_below),
        ]
        observed = [(value, limit) for value, limit in checks if value is not None]
        # Healthy only with data, and every threshold that applies to the data is met.
        m["healthy"] = bool(observed) and all(limit is None or value < limit for value, limit in observed)
        return m


class CloudWatchLogs:
    def __init__(self, client: Any = None, region: str | None = None):
        if client is None:
            import boto3

            client = boto3.client("logs", region_name=region)
        self.client = client

    def logs(self, entry: ServiceEntry, query: str = "", minutes: int = 15, limit: int = 20) -> list[str]:
        if not entry.log_group:
            return [f"No log group configured for {entry.name} in the service catalog"]
        kwargs: dict[str, Any] = {
            "logGroupName": entry.log_group,
            "startTime": int((time.time() - minutes * 60) * 1000),
            "limit": limit,
        }
        if query:
            kwargs["filterPattern"] = f'"{query}"'
        resp = self.client.filter_log_events(**kwargs)
        return [e["message"].rstrip() for e in resp.get("events", [])][-limit:]
