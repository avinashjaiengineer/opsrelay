"""SQLite -> DynamoDB migration keeps every audit chain verifiable."""

from moto import mock_aws

from opsrelay import audit, policy_admin
from opsrelay.store.dynamodb import DynamoStore
from opsrelay.store.migrate import migrate


def test_migration_copies_everything_and_chains_still_verify(service, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    incident = service.simulate("memory-leak")["incident"]
    [approval] = service.list_approvals()
    service.decide_approval(approval["id"], approve=True, approver="jane")
    service.simulate("bad-deploy")
    policy_admin.propose(service.store, "actions:\n  restart_service: {risk: low}\n", "ada", "")  # the policy chain

    with mock_aws():
        DynamoStore.create_table("opsrelay", "us-east-1")
        target = DynamoStore("opsrelay", "us-east-1")
        result = migrate(service.store, target)
        assert result["mismatched"] == [] and result["copied"]["incidents"] == 2
        assert result["copied"]["records"] >= 2  # memory, execution, policy...

        for chain in (incident["id"], "policy"):
            assert audit.verify_incident(target, chain)["ok"]
            assert audit.verify_incident(target, chain)["head"] == audit.verify_incident(service.store, chain)["head"]
        assert target.get_incident(incident["id"])["status"] == "resolved"
        assert target.get_record("memory", incident["id"])["action"] == "restart_service"

        again = migrate(service.store, target)  # rerunnable: nothing copied twice
        assert again["copied"]["incidents"] == 0 and again["copied"]["events"] == 0 and again["mismatched"] == []
