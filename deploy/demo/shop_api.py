# ruff: noqa: S311, S104 - demo traffic, not crypto; the container must listen on all interfaces
"""shop-api: a tiny demo web service for OpsRelay's real-workload demo (deploy/demo/demo.py).

Runs on ECS Fargate with the stock Python image (standard library only). It serves /checkout and
/health, drives its own steady traffic, and publishes Requests, Errors and LatencyP99Ms to
CloudWatch every 10 seconds through its logs (Embedded Metric Format), so no load balancer is needed.

A release is configured by environment variables:

    APP_VERSION   shown in logs            FAULT_RATE   share of checkouts that fail (0.0 - 1.0)
"""

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE = os.environ.get("SERVICE", "shop-api")
VERSION = os.environ.get("APP_VERSION", "1.0")
FAULT_RATE = float(os.environ.get("FAULT_RATE", "0"))
RPS = float(os.environ.get("RPS", "5"))
PORT = int(os.environ.get("PORT", "8080"))

lock = threading.Lock()
window = {"requests": 0, "errors": 0, "latencies": []}


def log(level: str, message: str) -> None:
    print(f"{level:<5} {SERVICE} v{VERSION} {message}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep logs to what matters
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        if self.path == "/health":
            return self._reply(200, {"status": "ok", "version": VERSION})
        if self.path.startswith("/checkout"):
            time.sleep(random.uniform(0.005, 0.02))
            if random.random() < FAULT_RATE:
                time.sleep(random.uniform(0.2, 0.6))
                log(
                    "ERROR", "POST /checkout status=500 KeyError: 'promo_v2' in PromoEngine.apply (pricing/promo.py:88)"
                )
                return self._reply(500, {"error": "internal error"})
            return self._reply(200, {"order": random.randint(10000, 99999)})
        return self._reply(404, {"error": "not found"})

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def traffic() -> None:
    while True:
        started = time.monotonic()
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/checkout", timeout=5).read()
            failed = False
        except urllib.error.HTTPError:
            failed = True
        except Exception:  # noqa: BLE001 - count anything else as a failure too
            failed = True
        elapsed_ms = (time.monotonic() - started) * 1000
        with lock:
            window["requests"] += 1
            window["errors"] += failed
            window["latencies"].append(elapsed_ms)
        time.sleep(max(0.0, 1 / RPS - (time.monotonic() - started)))


def publish() -> None:
    while True:
        time.sleep(10)
        with lock:
            snapshot = dict(window)
            window.update(requests=0, errors=0, latencies=[])
        latencies = sorted(snapshot["latencies"]) or [0.0]
        p99 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.99))]
        print(
            json.dumps(
                {
                    "_aws": {
                        "Timestamp": int(time.time() * 1000),
                        "CloudWatchMetrics": [
                            {
                                "Namespace": "OpsRelay/Demo",
                                "Dimensions": [["Service"]],
                                "Metrics": [
                                    {"Name": "Requests", "Unit": "Count"},
                                    {"Name": "Errors", "Unit": "Count"},
                                    {"Name": "LatencyP99Ms", "Unit": "Milliseconds"},
                                ],
                            }
                        ],
                    },
                    "Service": SERVICE,
                    "Version": VERSION,
                    "Requests": snapshot["requests"],
                    "Errors": snapshot["errors"],
                    "LatencyP99Ms": round(p99, 1),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    log("INFO", f"starting (fault rate {FAULT_RATE:.0%}, {RPS:g} requests/s)")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=publish, daemon=True).start()
    traffic()
