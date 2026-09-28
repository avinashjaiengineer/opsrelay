"""The event boundary on AWS: Lambdas that turn queue messages into coordinator calls.

    MODE=intake   SQS alert queue (EventBridge CloudWatch alarms, SNS)  -> ingest_alert
    MODE=jobs     SQS work queue (job ids)                              -> run_job
    MODE=recover  EventBridge schedule                                  -> recover

Each call is an InvokeAgentRuntime request to the coordinator, SigV4-signed with the function's IAM
role. It uses only the Lambda runtime's botocore (no packaged dependencies). Failed messages are
reported individually (ReportBatchItemFailures), so SQS retries just those and, after the queue's
maxReceiveCount, moves them to its dead-letter queue.
"""

import json
import os
import urllib.parse
import urllib.request
import uuid

import botocore.session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


def invoke(payload: dict) -> dict:
    """Call the coordinator runtime with a JSON payload and return its JSON response."""
    arn = os.environ["COORDINATOR_ARN"]
    region = arn.split(":")[3]
    url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{urllib.parse.quote(arn, safe='')}/invocations?qualifier=DEFAULT"
    )
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        SESSION_HEADER: f"opsrelay-{os.environ.get('MODE', 'lambda')}-{uuid.uuid4().hex}",
    }
    request = AWSRequest(method="POST", url=url, data=body, headers=headers)
    credentials = botocore.session.get_session().get_credentials().get_frozen_credentials()
    SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(request)
    prepared = urllib.request.Request(url, data=body, headers=dict(request.headers), method="POST")
    with urllib.request.urlopen(prepared, timeout=int(os.environ.get("CALL_TIMEOUT", "840"))) as resp:  # noqa: S310
        return json.loads(resp.read() or b"{}")


def _job_done(result: dict) -> bool:
    # "in_progress": another worker holds the lease; retry later so a stuck job is picked up.
    return result.get("status") in ("done", "failed", "unknown")


def handler(event: dict, context: object) -> dict:
    mode = os.environ.get("MODE", "intake")
    if mode == "recover":
        return invoke({"action": "recover"})

    failures = []
    for record in event.get("Records", []):
        try:
            if mode == "jobs":
                result = invoke({"action": "run_job", "job_id": json.loads(record["body"])["job_id"]})
                ok = "error" not in result and _job_done(result)
            else:
                result = invoke({"action": "ingest_alert", "message": record["body"]})
                ok = "error" not in result
            print(json.dumps({"message_id": record["messageId"], "result": result})[:2000])
        except Exception as e:  # noqa: BLE001 - reported per message; SQS retries it
            print(json.dumps({"message_id": record.get("messageId"), "error": f"{type(e).__name__}: {e}"}))
            ok = False
        if not ok:
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
