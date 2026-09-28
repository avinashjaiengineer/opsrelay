"""An SQS consumer for hosts that run OpsRelay continuously (EC2, `opsrelay up`).

    CloudWatch alarm -> EventBridge rule -> SQS queue (+ DLQ) -> this consumer -> intake router
    Alertmanager     -> SNS topic        -> SQS queue ---------^

A message is deleted only after its alerts are routed. A message that can't be routed stays on the
queue; SQS makes it visible again, and after the queue's maxReceiveCount the redrive policy moves it
to the dead-letter queue, where it can be inspected. (On AgentCore, a Lambda consumes the queue
instead: infra/lambda/intake.py.)
"""

import logging
import threading
from collections.abc import Callable
from typing import Any

from .alerts import UnrecognizedAlert

log = logging.getLogger(__name__)


class SqsIntake:
    def __init__(
        self, queue_url: str, service_factory: Callable[[], Any], client: Any = None, region: str | None = None
    ):
        if client is None:
            import boto3

            client = boto3.client("sqs", region_name=region)
        self.client = client
        self.queue_url = queue_url
        self.service_factory = service_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self, wait_seconds: int = 20) -> int:
        """Receive one batch and route it. Returns how many messages were handled (and deleted)."""
        resp = self.client.receive_message(
            QueueUrl=self.queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=wait_seconds, VisibilityTimeout=120
        )
        handled = 0
        for message in resp.get("Messages", []):
            try:
                results = self.service_factory().ingest(message["Body"])
            except UnrecognizedAlert as e:
                log.warning(
                    "intake: unrecognized message %s (%s); leaving it for the dead-letter queue",
                    message["MessageId"],
                    e,
                )
                continue
            except Exception:  # noqa: BLE001 - retried by SQS, then dead-lettered
                log.exception("intake: failed to route message %s", message["MessageId"])
                continue
            self.client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])
            log.info("intake: %s", results)
            handled += 1
        return handled

    def start(self) -> "SqsIntake":
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="opsrelay-intake", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - keep consuming
                log.exception("intake: polling failed")
                self._stop.wait(5)
