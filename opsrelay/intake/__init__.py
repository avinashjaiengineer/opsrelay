"""Event-driven incident intake.

- alerts.py:  one Alert model; parsers for CloudWatch alarm events (EventBridge or SNS) and
              Alertmanager webhooks
- router.py:  deduplication by fingerprint, correlation by service, incident creation
- sqs.py:     an SQS consumer for hosts that run OpsRelay continuously (EC2, `opsrelay up`)

On AgentCore, a Lambda consumes the SQS queue and calls the coordinator's `ingest_alert` action
(infra/lambda/intake.py).
"""
