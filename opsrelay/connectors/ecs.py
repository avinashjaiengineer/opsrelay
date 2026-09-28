"""Deployments and remediation actions on Amazon ECS services.

    rollback_deployment -> update the service to the task definition it ran before: the
                           `opsrelay:previous` tag your deploy pipeline sets, else revision - 1
    restart_service     -> force a new deployment (replaces every task)
    scale_service       -> set the desired count

Before changing anything, the intended change is tagged on the ECS service (`opsrelay:last-change`
= the execution's idempotency key, `opsrelay:target` = the target state). After a crash,
`reconcile` compares the service's actual state with that target:

    tag missing or for another change -> "not_applied" (safe to run)
    target reached                    -> "applied"     (don't repeat it)
    intent recorded, target not met   -> "unknown"     (a person checks; never act twice blindly)

Restarts are the exception: restarting twice is harmless, so an interrupted restart just runs again.
"""

import time
from typing import Any

from .catalog import ServiceEntry

TAG = "opsrelay:last-change"
TARGET = "opsrelay:target"
PREVIOUS = "opsrelay:previous"  # set by the deploy pipeline: the task definition before this release


class EcsDeployer:
    def __init__(self, client: Any = None, region: str | None = None, sleep: Any = None):
        if client is None:
            import boto3

            client = boto3.client("ecs", region_name=region)
        self.client = client
        self.sleep = sleep or time.sleep

    def _service(self, entry: ServiceEntry) -> dict:
        if not entry.ecs.get("cluster") or not entry.ecs.get("service"):
            raise KeyError(f"{entry.name} has no ECS cluster/service in the service catalog")
        resp = self.client.describe_services(cluster=entry.ecs["cluster"], services=[entry.ecs["service"]])
        if not resp.get("services"):
            raise KeyError(f"ECS service {entry.ecs['service']} not found in cluster {entry.ecs['cluster']}")
        return resp["services"][0]

    @staticmethod
    def _revision(task_definition_arn: str) -> tuple[str, int]:
        family_rev = task_definition_arn.rsplit("/", 1)[-1]
        family, rev = family_rev.rsplit(":", 1)
        return family, int(rev)

    def current_version(self, entry: ServiceEntry) -> str:
        family, rev = self._revision(self._service(entry)["taskDefinition"])
        return f"{family}:{rev}"

    def replicas(self, entry: ServiceEntry) -> int:
        return int(self._service(entry)["desiredCount"])

    def _tags(self, svc: dict) -> dict[str, str]:
        resp = self.client.list_tags_for_resource(resourceArn=svc["serviceArn"])
        return {t["key"]: t["value"] for t in resp.get("tags", [])}

    def _previous(self, svc: dict) -> str | None:
        """The task definition (family:revision) that ran before the current one."""
        try:
            tagged = self._tags(svc).get(PREVIOUS)
        except Exception:  # noqa: BLE001 - fall back to the revision number
            tagged = None
        family, rev = self._revision(svc["taskDefinition"])
        if tagged and tagged != f"{family}:{rev}":
            return tagged
        return f"{family}:{rev - 1}" if rev > 1 else None

    def deployments(self, entry: ServiceEntry) -> list[dict]:
        """Oldest first, the running one last: the previous task definition, then the current one."""
        svc = self._service(entry)
        family, rev = self._revision(svc["taskDefinition"])
        versions = [v for v in (self._previous(svc), f"{family}:{rev}") if v]
        history = [{"version": v, "at": None, "by": "ecs"} for v in versions]
        running = svc.get("deployments", [])
        if running and history:
            history[-1]["at"] = str(running[0].get("createdAt") or "")
        return history

    def _settle(self, entry: ServiceEntry) -> None:
        """Wait for the rollout to finish, then long enough for metrics to come from the new tasks."""
        if entry.settle_seconds <= 0:
            return
        self.client.get_waiter("services_stable").wait(
            cluster=entry.ecs["cluster"],
            services=[entry.ecs["service"]],
            WaiterConfig={"Delay": 10, "MaxAttempts": 30},
        )
        self.sleep(entry.settle_seconds)

    def _record_intent(self, service: dict, key: str | None, target: str) -> None:
        if key:
            self.client.tag_resource(
                resourceArn=service["serviceArn"],
                tags=[{"key": TAG, "value": key[:256]}, {"key": TARGET, "value": target[:256]}],
            )

    def execute(self, entry: ServiceEntry, action: str, params: dict, key: str | None) -> dict:
        svc = self._service(entry)
        cluster, name = entry.ecs["cluster"], entry.ecs["service"]
        if action == "rollback_deployment":
            target = self._previous(svc)
            if target is None:
                return {"ok": False, "detail": f"{entry.name} has no previous task definition to roll back to"}
            if params.get("target_version") not in (None, target):
                return {"ok": False, "detail": f"{entry.name} can only roll back to {target}"}
            self._record_intent(svc, key, f"taskDefinition={target}")
            self.client.update_service(cluster=cluster, service=name, taskDefinition=target)
            detail = f"Rolled {entry.name} back to {target}"
        elif action == "restart_service":
            self._record_intent(svc, key, "restart")
            self.client.update_service(cluster=cluster, service=name, forceNewDeployment=True)
            detail = f"Started a rolling restart of {entry.name}"
        elif action == "scale_service":
            replicas = int(params.get("replicas", 0))
            if not 1 <= replicas <= entry.max_replicas:
                return {"ok": False, "detail": f"replicas must be between 1 and {entry.max_replicas}"}
            self._record_intent(svc, key, f"desiredCount={replicas}")
            self.client.update_service(cluster=cluster, service=name, desiredCount=replicas)
            detail = f"Scaled {entry.name} to {replicas} tasks"
        else:
            return {"ok": False, "detail": f"{action} is not supported on ECS"}
        try:
            self._settle(entry)
        except Exception as e:  # noqa: BLE001 - the change was made; verification will judge it
            detail += f" (rollout not confirmed stable: {type(e).__name__})"
        return {"ok": True, "detail": detail}

    def reconcile(self, entry: ServiceEntry, key: str) -> str:
        try:
            svc = self._service(entry)
            tags = self._tags(svc)
        except Exception:  # noqa: BLE001 - if we can't look, we don't know
            return "unknown"
        if tags.get(TAG) != key[:256]:
            return "not_applied"
        target = tags.get(TARGET, "")
        if target == "restart":
            return "not_applied"  # restarting again is harmless
        field, _, value = target.partition("=")
        if field == "taskDefinition":
            family, rev = self._revision(svc["taskDefinition"])
            return "applied" if f"{family}:{rev}" == value else "unknown"
        if field == "desiredCount":
            return "applied" if str(svc["desiredCount"]) == value else "unknown"
        return "unknown"
