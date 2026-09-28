"""DynamoDB single-table store, shared by every AgentCore Runtime.

Items (each keeps the record as a JSON string in `doc`, so no float/Decimal conversion):

    pk                 sk                           gsi1pk        gsi1sk          other
    INC#<id>           META                         INCIDENT      <created_at>    status
    INC#<id>           EVT#<position in chain>      -             -
    INC#<id>           CHAIN                        -             -               head (last hash), n (events)
    APR#<id>           META                         APPROVAL      <created_at>    status
    REC#<kind>#<id>    META                         REC#<kind>    <created_at>    status, rev
    SVC#<name>         META                         SERVICE       <name>

`commit` is one TransactWriteItems call: incident and approval writes are conditioned on their
status, records on their revision, new items on not existing, and each touched incident's CHAIN
item on its previous head, so concurrent writers can't fork the hash chain. If the transaction is
cancelled, the conditions are re-read: a real conflict returns None, a lost race on a chain head
is retried.
"""

import json
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from .base import GENESIS_HASH, Commit, Committed, Record, Store, now_iso, seal

GSI = "gsi1"
MAX_ATTEMPTS = 20


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


def _cancelled(e: ClientError) -> bool:
    return e.response["Error"]["Code"] in ("ConditionalCheckFailedException", "TransactionCanceledException")


class DynamoStore(Store):
    def __init__(self, table_name: str, region: str | None = None, resource: Any = None, endpoint: str | None = None):
        dynamodb = resource or _resource(region, endpoint)
        self.table = dynamodb.Table(table_name)
        # The resource's client serializes plain Python values itself (no {"S": ...} wrappers).
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

    @staticmethod
    def _incident_item(incident: Record) -> Record:
        return {
            "pk": f"INC#{incident['id']}",
            "sk": "META",
            "doc": json.dumps(incident),
            "status": incident["status"],
            "gsi1pk": "INCIDENT",
            "gsi1sk": incident["created_at"],
        }

    @staticmethod
    def _approval_item(approval: Record) -> Record:
        return {
            "pk": f"APR#{approval['id']}",
            "sk": "META",
            "doc": json.dumps(approval),
            "status": approval["status"],
            "gsi1pk": "APPROVAL",
            "gsi1sk": approval["created_at"],
        }

    @staticmethod
    def _record_item(record: Record) -> Record:
        return {
            "pk": f"REC#{record['kind']}#{record['id']}",
            "sk": "META",
            "doc": json.dumps(record),
            "status": record.get("status") or "-",
            "rev": record.get("rev", 0),
            "gsi1pk": f"REC#{record['kind']}",
            "gsi1sk": record["created_at"],
        }

    # Atomic writes
    def commit(self, change: Commit) -> Committed | None:
        table = self.table.name
        for _ in range(MAX_ATTEMPTS):
            items: list[dict] = []
            incident = None
            if change.incident:
                c = change.incident
                current = self.get_incident(c.incident_id)
                if current is None or current["status"] != c.expected_status:
                    return None
                incident = {**current, **c.updates}
                items.append(
                    {
                        "Put": {
                            "TableName": table,
                            "Item": self._incident_item(incident),
                            "ConditionExpression": "#s = :from",
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": {":from": c.expected_status},
                        }
                    }
                )

            approvals: dict[str, Record] = {}
            if any(self.get_approval(a["id"]) for a in change.new_approvals) or any(
                self.get_record(r["kind"], r["id"]) for r in change.new_records
            ):
                return None
            for a in change.new_approvals:
                items.append(
                    {
                        "Put": {
                            "TableName": table,
                            "Item": self._approval_item(a),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    }
                )
                approvals[a["id"]] = a
            for m in change.approval_moves:
                current = self.get_approval(m.approval_id)
                if current is None or current["status"] != m.expected_status:
                    return None
                approval = {**current, **m.updates}
                items.append(
                    {
                        "Put": {
                            "TableName": table,
                            "Item": self._approval_item(approval),
                            "ConditionExpression": "#s = :from",
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": {":from": m.expected_status},
                        }
                    }
                )
                approvals[m.approval_id] = approval

            records: dict[tuple[str, str], Record] = {}
            for r in change.new_records:
                items.append(
                    {
                        "Put": {
                            "TableName": table,
                            "Item": self._record_item(r),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    }
                )
                records[(r["kind"], r["id"])] = r
            for m in change.record_moves:
                current = self.get_record(m.kind, m.record_id)
                if current is None or current["rev"] != m.expected_rev:
                    return None
                record = {**current, **m.updates, "rev": m.expected_rev + 1, "updated_at": now_iso()}
                items.append(
                    {
                        "Put": {
                            "TableName": table,
                            "Item": self._record_item(record),
                            "ConditionExpression": "rev = :rev",
                            "ExpressionAttributeValues": {":rev": m.expected_rev},
                        }
                    }
                )
                records[(m.kind, m.record_id)] = record

            # Each event's sort key is its position in the chain (from the CHAIN item), so reading
            # events back always yields chain order, whatever order concurrent writers created them.
            sealed_events: list[Record] = []
            heads: dict[str, list] = {}  # incident -> [head read, current head, position]
            for event in change.events:
                pk = f"INC#{event['incident_id']}"
                if pk not in heads:
                    head_item = self._client.get_item(
                        TableName=table, Key={"pk": pk, "sk": "CHAIN"}, ConsistentRead=True
                    ).get("Item")
                    read = head_item["head"] if head_item else None
                    heads[pk] = [read, read or GENESIS_HASH, int(head_item.get("n", 0)) if head_item else 0]
                read, prev, n = heads[pk]
                sealed = seal(event, prev)
                heads[pk] = [read, sealed["hash"], n + 1]
                sk = f"EVT#{n + 1:012d}"
                items.append(
                    {
                        "Put": {
                            "TableName": table,
                            "Item": {"pk": pk, "sk": sk, "doc": json.dumps(sealed)},
                            "ConditionExpression": "attribute_not_exists(sk)",  # a position is written once
                        }
                    }
                )
                sealed_events.append(sealed)
            for pk, (read, head, n) in heads.items():
                update: dict[str, Any] = {
                    "TableName": table,
                    "Key": {"pk": pk, "sk": "CHAIN"},
                    "UpdateExpression": "SET head = :new, n = :n",
                    "ExpressionAttributeValues": {":new": head, ":n": n},
                }
                if read is None:
                    update["ConditionExpression"] = "attribute_not_exists(head)"
                else:
                    update["ConditionExpression"] = "head = :prev"
                    update["ExpressionAttributeValues"][":prev"] = read
                items.append({"Update": update})

            if not items:
                return Committed(incident=None, approvals={}, records={}, events=[])
            if len(items) > 100:
                raise ValueError(f"A commit can write at most 100 items, not {len(items)}")
            try:
                self._client.transact_write_items(TransactItems=items)
            except ClientError as e:
                if not _cancelled(e):
                    raise
                continue  # re-read: a real conflict returns None above; a chain-head race retries
            return Committed(incident=incident, approvals=approvals, records=records, events=sealed_events)
        return None

    # Incidents
    def put_incident(self, incident: Record) -> None:
        self.table.put_item(Item=self._incident_item(incident), ConditionExpression="attribute_not_exists(pk)")

    def get_incident(self, incident_id: str) -> Record | None:
        return self._get(f"INC#{incident_id}")

    def list_incidents(self, limit: int = 50) -> list[Record]:
        return self._query(Key("gsi1pk").eq("INCIDENT"), index=GSI, forward=False, limit=limit)

    # Audit log
    def list_events(self, incident_id: str) -> list[Record]:
        return self._query(Key("pk").eq(f"INC#{incident_id}") & Key("sk").begins_with("EVT#"))

    # Approvals
    def get_approval(self, approval_id: str) -> Record | None:
        return self._get(f"APR#{approval_id}")

    def list_approvals(self, status: str | None = None, incident_id: str | None = None) -> list[Record]:
        approvals = self._query(Key("gsi1pk").eq("APPROVAL"), index=GSI)
        return [
            a
            for a in approvals
            if (status is None or a["status"] == status) and (incident_id is None or a["incident_id"] == incident_id)
        ]

    # Records
    def get_record(self, kind: str, record_id: str) -> Record | None:
        return self._get(f"REC#{kind}#{record_id}")

    def list_records(self, kind: str, status: str | None = None, limit: int = 200) -> list[Record]:
        records = self._query(Key("gsi1pk").eq(f"REC#{kind}"), index=GSI, forward=False)
        return [r for r in records if status is None or r.get("status") == status][:limit]

    # Services
    def put_service(self, service: Record) -> None:
        self._put(f"SVC#{service['name']}", "META", service, gsi1pk="SERVICE", gsi1sk=service["name"])

    def get_service(self, name: str) -> Record | None:
        return self._get(f"SVC#{name}")

    def list_services(self) -> list[Record]:
        return self._query(Key("gsi1pk").eq("SERVICE"), index=GSI)
