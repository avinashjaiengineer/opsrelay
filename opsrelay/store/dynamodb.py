"""DynamoDB single-table store, shared by every AgentCore Runtime.

Items (each keeps the record as a JSON string in `doc`, so no float/Decimal conversion):

    pk              sk                           gsi1pk      gsi1sk          other
    INC#<id>        META                         INCIDENT    <created_at>    status
    INC#<id>        EVT#<created_at>#<seq>#<id>  -           -
    INC#<id>        CHAIN                        -           -               head (hash of the last event)
    APR#<id>        META                         APPROVAL    <created_at>    status
    EXE#<key>       META                         -           -
    DLQ#<id>        META                         DEADLETTER  <created_at>
    SVC#<name>      META                         SERVICE     <name>

Status changes are conditional writes on the `status` attribute. Each audit event is written in
one transaction with its incident's CHAIN item, conditioned on the previous head, so concurrent
writers can't fork the hash chain.
"""

import itertools
import json
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from .base import GENESIS_HASH, Record, Store, seal

GSI = "gsi1"

# Orders events written in the same millisecond by this process; created_at alone ties.
_event_seq = itertools.count()


def _resource(region: str | None, endpoint: str | None) -> Any:
    if endpoint:  # DynamoDB Local: never send real credentials to it
        return boto3.resource(
            "dynamodb",
            region_name=region or "us-east-1",
            endpoint_url=endpoint,
            aws_access_key_id="local",
            aws_secret_access_key="local",  # noqa: S106 - DynamoDB Local ignores credentials
        )
    return boto3.resource("dynamodb", region_name=region)


def _conditional_failed(e: ClientError) -> bool:
    return e.response["Error"]["Code"] in ("ConditionalCheckFailedException", "TransactionCanceledException")


class DynamoStore(Store):
    def __init__(self, table_name: str, region: str | None = None, resource: Any = None, endpoint: str | None = None):
        dynamodb = resource or _resource(region, endpoint)
        self.table = dynamodb.Table(table_name)
        self._client = self.table.meta.client

    @staticmethod
    def create_table(
        table_name: str, region: str | None = None, resource: Any = None, endpoint: str | None = None
    ) -> None:
        """For local development; on AWS the CDK stack creates the table."""
        dynamodb = resource or _resource(region, endpoint)
        dynamodb.create_table(
            TableName=table_name,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("pk", "sk", "gsi1pk", "gsi1sk")],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": GSI,
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        ).wait_until_exists()

    # Low-level helpers
    def _put(self, pk: str, sk: str, doc: Record, **attrs: str) -> None:
        self.table.put_item(Item={"pk": pk, "sk": sk, "doc": json.dumps(doc), **attrs})

    def _get(self, pk: str, sk: str = "META") -> Record | None:
        item = self.table.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")
        return json.loads(item["doc"]) if item else None

    def _query(self, key_condition: Any, index: str | None = None, forward: bool = True, limit: int | None = None):
        kwargs: dict[str, Any] = {"KeyConditionExpression": key_condition, "ScanIndexForward": forward}
        if index:
            kwargs["IndexName"] = index
        else:
            kwargs["ConsistentRead"] = True
        out: list[Record] = []
        while True:
            resp = self.table.query(**kwargs)
            out.extend(json.loads(i["doc"]) for i in resp["Items"])
            if (limit and len(out) >= limit) or "LastEvaluatedKey" not in resp:
                return out[:limit] if limit else out
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    # Incidents
    def _incident_item(self, incident: Record) -> Record:
        return {
            "pk": f"INC#{incident['id']}",
            "sk": "META",
            "doc": json.dumps(incident),
            "status": incident["status"],
            "gsi1pk": "INCIDENT",
            "gsi1sk": incident["created_at"],
        }

    def put_incident(self, incident: Record) -> None:
        self.table.put_item(Item=self._incident_item(incident), ConditionExpression="attribute_not_exists(pk)")

    def get_incident(self, incident_id: str) -> Record | None:
        return self._get(f"INC#{incident_id}")

    def list_incidents(self, limit: int = 50) -> list[Record]:
        return self._query(Key("gsi1pk").eq("INCIDENT"), index=GSI, forward=False, limit=limit)

    def transition_incident(self, incident_id: str, from_status: str, updates: Record) -> Record | None:
        current = self.get_incident(incident_id)
        if current is None or current["status"] != from_status:
            return None
        incident = {**current, **updates}
        try:
            self.table.put_item(
                Item=self._incident_item(incident),
                ConditionExpression="#s = :from",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":from": from_status},
            )
        except ClientError as e:
            if _conditional_failed(e):
                return None
            raise
        return incident

    # Audit log
    def append_event(self, event: Record) -> Record:
        # The resource's client serializes plain Python values itself (no {"S": ...} wrappers).
        pk = f"INC#{event['incident_id']}"
        table = self.table.name
        for _ in range(20):
            head_item = self._client.get_item(TableName=table, Key={"pk": pk, "sk": "CHAIN"}, ConsistentRead=True).get(
                "Item"
            )
            prev_hash = head_item["head"] if head_item else GENESIS_HASH
            sealed = seal(event, prev_hash)
            sk = f"EVT#{event['created_at']}#{next(_event_seq):012d}#{event['id']}"
            head_update: dict[str, Any] = {
                "TableName": table,
                "Key": {"pk": pk, "sk": "CHAIN"},
                "UpdateExpression": "SET head = :new",
                "ExpressionAttributeValues": {":new": sealed["hash"]},
            }
            if head_item:
                head_update["ConditionExpression"] = "head = :prev"
                head_update["ExpressionAttributeValues"][":prev"] = prev_hash
            else:
                head_update["ConditionExpression"] = "attribute_not_exists(head)"
            try:
                self._client.transact_write_items(
                    TransactItems=[
                        {"Put": {"TableName": table, "Item": {"pk": pk, "sk": sk, "doc": json.dumps(sealed)}}},
                        {"Update": head_update},
                    ]
                )
                return sealed
            except ClientError as e:
                if not _conditional_failed(e):
                    raise
        raise RuntimeError(f"Could not append to the audit chain of {event['incident_id']}")

    def list_events(self, incident_id: str) -> list[Record]:
        return self._query(Key("pk").eq(f"INC#{incident_id}") & Key("sk").begins_with("EVT#"))

    # Approvals
    def _approval_item(self, approval: Record) -> Record:
        return {
            "pk": f"APR#{approval['id']}",
            "sk": "META",
            "doc": json.dumps(approval),
            "status": approval["status"],
            "gsi1pk": "APPROVAL",
            "gsi1sk": approval["created_at"],
        }

    def put_approval(self, approval: Record) -> None:
        self.table.put_item(Item=self._approval_item(approval))

    def get_approval(self, approval_id: str) -> Record | None:
        return self._get(f"APR#{approval_id}")

    def list_approvals(self, status: str | None = None, incident_id: str | None = None) -> list[Record]:
        approvals = self._query(Key("gsi1pk").eq("APPROVAL"), index=GSI)
        return [
            a
            for a in approvals
            if (status is None or a["status"] == status) and (incident_id is None or a["incident_id"] == incident_id)
        ]

    def transition_approval(self, approval_id: str, from_status: str, updates: Record) -> Record | None:
        current = self.get_approval(approval_id)
        if current is None or current["status"] != from_status:
            return None
        approval = {**current, **updates}
        try:
            self.table.put_item(
                Item=self._approval_item(approval),
                ConditionExpression="#s = :from",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":from": from_status},
            )
        except ClientError as e:
            if _conditional_failed(e):
                return None
            raise
        return approval

    # Executions
    def claim_execution(self, execution: Record) -> bool:
        try:
            self.table.put_item(
                Item={"pk": f"EXE#{execution['key']}", "sk": "META", "doc": json.dumps(execution)},
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as e:
            if _conditional_failed(e):
                return False
            raise
        return True

    def get_execution(self, key: str) -> Record | None:
        return self._get(f"EXE#{key}")

    def finish_execution(self, key: str, updates: Record) -> Record:
        current = self.get_execution(key)
        if current is None:
            raise KeyError(f"Unknown execution {key}")
        execution = {**current, **updates}
        self._put(f"EXE#{key}", "META", execution)
        return execution

    # Dead letters
    def put_dead_letter(self, dead_letter: Record) -> None:
        self._put(
            f"DLQ#{dead_letter['id']}", "META", dead_letter, gsi1pk="DEADLETTER", gsi1sk=dead_letter["created_at"]
        )

    def list_dead_letters(self, limit: int = 50) -> list[Record]:
        return self._query(Key("gsi1pk").eq("DEADLETTER"), index=GSI, forward=False, limit=limit)

    # Services
    def put_service(self, service: Record) -> None:
        self._put(f"SVC#{service['name']}", "META", service, gsi1pk="SERVICE", gsi1sk=service["name"])

    def get_service(self, name: str) -> Record | None:
        return self._get(f"SVC#{name}")

    def list_services(self) -> list[Record]:
        return self._query(Key("gsi1pk").eq("SERVICE"), index=GSI)
