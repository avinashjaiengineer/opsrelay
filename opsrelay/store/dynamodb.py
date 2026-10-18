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
import time
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, EndpointConnectionError

from .base import GENESIS_HASH, Commit, Committed, Page, Record, Store, decode_cursor, encode_cursor, now_iso, seal

GSI = "gsi1"
STATUS_GSI = "gsi2"
INCIDENT_APPROVAL_GSI = "gsi3"
REQUIRED_INDEXES = frozenset((GSI, STATUS_GSI, INCIDENT_APPROVAL_GSI))
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

    def schema_health(self) -> Record:
        """Report whether the deployed table has every index used by the store."""
        description = self._client.describe_table(TableName=self.table.name)["Table"]
        indexes = {index["IndexName"] for index in description.get("GlobalSecondaryIndexes", [])}
        missing = sorted(REQUIRED_INDEXES - indexes)
        return {
            "ok": not missing,
            "backend": type(self).__name__,
            "table": self.table.name,
            "indexes": sorted(indexes),
            "missing_indexes": missing,
        }

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
            AttributeDefinitions=[
                {"AttributeName": n, "AttributeType": "S"}
                for n in ("pk", "sk", "gsi1pk", "gsi1sk", "gsi2pk", "gsi2sk", "gsi3pk", "gsi3sk")
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": GSI,
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": STATUS_GSI,
                    "KeySchema": [
                        {"AttributeName": "gsi2pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi2sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": INCIDENT_APPROVAL_GSI,
                    "KeySchema": [
                        {"AttributeName": "gsi3pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi3sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        ).wait_until_exists()

    @staticmethod
    def ensure_table(
        table_name: str,
        region: str | None = None,
        resource: Any = None,
        endpoint: str | None = None,
        attempts: int = 30,
        delay_seconds: float = 1.0,
    ) -> None:
        """Create a table when needed, retrying while a local endpoint becomes ready."""
        for attempt in range(attempts):
            try:
                DynamoStore.create_table(table_name, region, resource, endpoint)
                return
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "ResourceInUseException":
                    return
                if attempt == attempts - 1:
                    raise
            except EndpointConnectionError:
                if attempt == attempts - 1:
                    raise
            time.sleep(delay_seconds)

    def backfill_status_index(self) -> int:
        """Populate gsi2 keys on legacy approval and record items; return items updated."""
        updated = 0
        kwargs: dict[str, Any] = {
            "ProjectionExpression": "#pk, #sk, #doc",
            "ExpressionAttributeNames": {"#pk": "pk", "#sk": "sk", "#doc": "doc"},
        }
        while True:
            response = self.table.scan(**kwargs)
            for item in response.get("Items", []):
                pk = item.get("pk", "")
                if not (pk.startswith("APR#") or pk.startswith("REC#")):
                    continue
                doc = json.loads(item["doc"])
                status = doc.get("status")
                created_at = doc.get("created_at")
                if not status or not created_at:
                    continue
                kind = doc.get("kind") if pk.startswith("REC#") else "APPROVAL"
                index_pk = f"REC#{kind}#{status}" if kind != "APPROVAL" else f"APPROVAL#{status}"
                updates = []
                values: dict[str, str] = {}
                if "gsi2pk" not in item or "gsi2sk" not in item:
                    updates.extend(["gsi2pk = :status_pk", "gsi2sk = :status_sk"])
                    values.update({":status_pk": index_pk, ":status_sk": created_at})
                if kind == "APPROVAL" and ("gsi3pk" not in item or "gsi3sk" not in item):
                    updates.extend(["gsi3pk = :incident_pk", "gsi3sk = :incident_sk"])
                    values.update({":incident_pk": f"APPROVAL#{doc['incident_id']}", ":incident_sk": created_at})
                if not updates:
                    continue
                self.table.update_item(
                    Key={"pk": pk, "sk": item["sk"]},
                    UpdateExpression="SET " + ", ".join(updates),
                    ExpressionAttributeValues=values,
                )
                updated += 1
            if "LastEvaluatedKey" not in response:
                return updated
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

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
            "rev": int(incident.get("rev", 0)),
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
            "gsi2pk": f"APPROVAL#{approval['status']}",
            "gsi2sk": approval["created_at"],
            "gsi3pk": f"APPROVAL#{approval['incident_id']}",
            "gsi3sk": approval["created_at"],
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
            "gsi2pk": f"REC#{record['kind']}#{record.get('status') or '-'}",
            "gsi2sk": record["created_at"],
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
                incident["rev"] = int(current.get("rev", 0)) + 1
                items.append(
                    {
                        "Put": {
                            "TableName": table,
                            "Item": self._incident_item(incident),
                            "ConditionExpression": "#s = :from AND (attribute_not_exists(#r) OR #r = :rev)",
                            "ExpressionAttributeNames": {"#s": "status", "#r": "rev"},
                            "ExpressionAttributeValues": {
                                ":from": c.expected_status,
                                ":rev": int(c.expected_rev if c.expected_rev is not None else current.get("rev", 0)),
                            },
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
        incident = {**incident, "rev": int(incident.get("rev", 0))}
        self.table.put_item(Item=self._incident_item(incident), ConditionExpression="attribute_not_exists(pk)")

    def get_incident(self, incident_id: str) -> Record | None:
        return self._get(f"INC#{incident_id}")

    def list_incidents(self, limit: int = 50) -> list[Record]:
        return self._query(Key("gsi1pk").eq("INCIDENT"), index=GSI, forward=False, limit=limit)

    def list_incidents_page(self, limit: int = 50, cursor: str | None = None) -> Page:
        kwargs: dict[str, Any] = {
            "IndexName": GSI,
            "KeyConditionExpression": Key("gsi1pk").eq("INCIDENT"),
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if cursor:
            start_key = decode_cursor(cursor)
            if not isinstance(start_key, dict):
                raise ValueError("invalid cursor")
            kwargs["ExclusiveStartKey"] = start_key
        response = self.table.query(**kwargs)
        items = [json.loads(item["doc"]) for item in response.get("Items", [])]
        next_cursor = encode_cursor(response["LastEvaluatedKey"]) if response.get("LastEvaluatedKey") else None
        return Page(items, next_cursor)

    # Audit log
    def list_events(self, incident_id: str) -> list[Record]:
        return self._query(Key("pk").eq(f"INC#{incident_id}") & Key("sk").begins_with("EVT#"))

    def list_events_page(self, incident_id: str, limit: int = 100, cursor: str | None = None) -> Page:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(f"INC#{incident_id}") & Key("sk").begins_with("EVT#"),
            "ScanIndexForward": True,
            "Limit": limit,
        }
        if cursor:
            start_key = decode_cursor(cursor)
            if not isinstance(start_key, dict):
                raise ValueError("invalid cursor")
            kwargs["ExclusiveStartKey"] = start_key
        response = self.table.query(**kwargs)
        items = [json.loads(item["doc"]) for item in response.get("Items", [])]
        next_cursor = encode_cursor(response["LastEvaluatedKey"]) if response.get("LastEvaluatedKey") else None
        return Page(items, next_cursor)

    def event_count(self, incident_id: str) -> int:
        item = self.table.get_item(Key={"pk": f"INC#{incident_id}", "sk": "CHAIN"}, ConsistentRead=True).get("Item")
        return int(item.get("n", 0)) if item else 0

    # Approvals
    def get_approval(self, approval_id: str) -> Record | None:
        return self._get(f"APR#{approval_id}")

    def list_approvals(self, status: str | None = None, incident_id: str | None = None) -> list[Record]:
        if incident_id:
            approvals = self._query(Key("gsi3pk").eq(f"APPROVAL#{incident_id}"), index=INCIDENT_APPROVAL_GSI)
        elif status:
            approvals = self._query(Key("gsi2pk").eq(f"APPROVAL#{status}"), index=STATUS_GSI)
        else:
            approvals = self._query(Key("gsi1pk").eq("APPROVAL"), index=GSI)
        return [
            a
            for a in approvals
            if (status is None or a["status"] == status) and (incident_id is None or a["incident_id"] == incident_id)
        ]

    def list_approvals_page(
        self, status: str | None = None, incident_id: str | None = None, limit: int = 50, cursor: str | None = None
    ) -> Page:
        if incident_id:
            index, key_name, key_value = INCIDENT_APPROVAL_GSI, "gsi3pk", f"APPROVAL#{incident_id}"
        else:
            index = STATUS_GSI if status else GSI
            key_name = "gsi2pk" if status else "gsi1pk"
            key_value = f"APPROVAL#{status}" if status else "APPROVAL"
        kwargs: dict[str, Any] = {
            "IndexName": index,
            "KeyConditionExpression": Key(key_name).eq(key_value),
            "ScanIndexForward": True,
            "Limit": limit,
        }
        if cursor:
            start_key = decode_cursor(cursor)
            if not isinstance(start_key, dict):
                raise ValueError("invalid cursor")
            kwargs["ExclusiveStartKey"] = start_key
        response = self.table.query(**kwargs)
        items = [json.loads(item["doc"]) for item in response.get("Items", [])]
        next_cursor = encode_cursor(response["LastEvaluatedKey"]) if response.get("LastEvaluatedKey") else None
        return Page(items, next_cursor)

    # Records
    def get_record(self, kind: str, record_id: str) -> Record | None:
        return self._get(f"REC#{kind}#{record_id}")

    def list_records(self, kind: str, status: str | None = None, limit: int = 200) -> list[Record]:
        if status:
            return self._query(Key("gsi2pk").eq(f"REC#{kind}#{status}"), index=STATUS_GSI, forward=False, limit=limit)
        return self._query(Key("gsi1pk").eq(f"REC#{kind}"), index=GSI, forward=False, limit=limit)

    def list_records_page(
        self, kind: str, status: str | None = None, limit: int = 200, cursor: str | None = None
    ) -> Page:
        index = STATUS_GSI if status else GSI
        key_name = "gsi2pk" if status else "gsi1pk"
        key_value = f"REC#{kind}#{status}" if status else f"REC#{kind}"
        kwargs: dict[str, Any] = {
            "IndexName": index,
            "KeyConditionExpression": Key(key_name).eq(key_value),
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if cursor:
            start_key = decode_cursor(cursor)
            if not isinstance(start_key, dict):
                raise ValueError("invalid cursor")
            kwargs["ExclusiveStartKey"] = start_key
        response = self.table.query(**kwargs)
        items = [json.loads(item["doc"]) for item in response.get("Items", [])]
        next_cursor = encode_cursor(response["LastEvaluatedKey"]) if response.get("LastEvaluatedKey") else None
        return Page(items, next_cursor)

    # Services
    def put_service(self, service: Record) -> None:
        self._put(f"SVC#{service['name']}", "META", service, gsi1pk="SERVICE", gsi1sk=service["name"])

    def get_service(self, name: str) -> Record | None:
        return self._get(f"SVC#{name}")

    def list_services(self) -> list[Record]:
        return self._query(Key("gsi1pk").eq("SERVICE"), index=GSI)
