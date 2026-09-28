"""Authentication (dev tokens, OIDC JWTs), role-based authorization, and audited policy changes."""

import json
import time

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from opsrelay import auth, policy_admin, rbac
from opsrelay.config import get_settings
from opsrelay.policy import get_policy
from opsrelay.runtime import coordinator

TOKENS = {
    "jane": "tok-jane-ic",
    "sam": "tok-sam-sre",
    "olga": "tok-olga-operator",
    "ada": "tok-ada-admin",
    "bob": "tok-bob-admin",
    "vic": "tok-vic-viewer",
}
USERS = [
    {"name": "Jane", "roles": ["sre", "incident_commander"], "token_sha256": auth.token_hash(TOKENS["jane"])},
    {"name": "Sam", "roles": ["sre"], "token_sha256": auth.token_hash(TOKENS["sam"])},
    {"name": "Olga", "roles": ["operator"], "token_sha256": auth.token_hash(TOKENS["olga"])},
    {"name": "Ada", "roles": ["admin"], "token_sha256": auth.token_hash(TOKENS["ada"])},
    {"name": "Bob", "roles": ["admin"], "token_sha256": auth.token_hash(TOKENS["bob"])},
    {"name": "Vic", "roles": ["viewer"], "token_sha256": auth.token_hash(TOKENS["vic"])},
]


@pytest.fixture
def dev_mode(monkeypatch, tmp_path):
    users = tmp_path / "users.yaml"
    users.write_text(yaml.safe_dump({"users": USERS}))
    monkeypatch.setenv("OPSRELAY_AUTH_MODE", "dev")
    monkeypatch.setenv("OPSRELAY_DEV_USERS", str(users))
    get_settings.cache_clear()
    auth.clear_caches()
    monkeypatch.setattr(coordinator, "_service", None)
    with TestClient(coordinator.app) as client:
        yield client
    auth.clear_caches()


def _call(client, payload, who=None):
    headers = {"Authorization": f"Bearer {TOKENS[who]}"} if who else {}
    resp = client.post("/invocations", json=payload, headers=headers)
    return resp.status_code, resp.json()


def test_dev_tokens_identify_users(dev_mode):
    assert _call(dev_mode, {"action": "whoami"})[0] == 401
    assert (
        dev_mode.post("/invocations", json={"action": "whoami"}, headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )
    status, body = _call(dev_mode, {"action": "whoami"}, "jane")
    assert status == 200
    assert body["principal"] == {
        "name": "Jane",
        "roles": ["incident_commander", "sre"],
        "method": "dev",
        "verified": True,
    }
    assert dev_mode.get("/ping").status_code == 200  # health checks stay public


def test_roles_limit_actions(dev_mode):
    assert "forbidden" in _call(dev_mode, {"action": "simulate", "scenario": "bad-deploy"}, "vic")[1]["error"]
    assert "forbidden" in _call(dev_mode, {"action": "propose_policy", "text": "x"}, "jane")[1]["error"]
    assert "forbidden" in _call(dev_mode, {"action": "verify_audit", "incident_id": "x"}, "olga")[1]["error"]
    assert "incidents" in _call(dev_mode, {"action": "list_incidents"}, "vic")[1]


def test_approver_is_the_verified_caller_with_the_right_role(dev_mode):
    opened = _call(dev_mode, {"action": "simulate", "scenario": "bad-deploy"}, "olga")[1]  # SEV1, high risk
    [approval] = _call(dev_mode, {"action": "list_approvals"}, "olga")[1]["approvals"]
    decide = {"action": "decide_approval", "approval_id": approval["id"], "approve": True, "approver": "Mallory"}

    # An SRE may approve high risk, but not on a SEV1 incident; an operator not at all.
    assert "incident_commander" in _call(dev_mode, decide, "sam")[1]["error"]
    assert "forbidden" in _call(dev_mode, decide, "olga")[1]["error"]

    decided = _call(dev_mode, decide, "jane")[1]
    a = decided["approval"]
    # The payload's "Mallory" is ignored: the approver is the authenticated caller.
    assert (a["decided_by"], a["decided_by_role"], a["identity_verified"]) == ("Jane", "incident_commander", True)
    events = _call(dev_mode, {"action": "get_incident", "incident_id": opened["incident"]["id"]}, "vic")[1]["events"]
    approved = next(e for e in events if e["kind"] == "approval.approved")
    assert (approved["actor"], approved["data"]["identity_verified"]) == ("Jane", True)


def test_roles_needed_to_decide():
    assert rbac.roles_to_decide("low", "SEV3") == {"operator", "sre", "incident_commander"}
    assert rbac.roles_to_decide("high", "SEV2") == {"sre", "incident_commander"}
    assert rbac.roles_to_decide("low", "SEV1") == {"incident_commander"}
    assert rbac.roles_to_decide("critical", "SEV4") == {"incident_commander"}


def test_policy_changes_need_a_second_admin_and_are_audited(dev_mode):
    text = "version: 2\nactions:\n  restart_service: {risk: low, requires_approval: false}\n"
    proposed = _call(dev_mode, {"action": "propose_policy", "text": text, "note": "restarts are safe"}, "ada")[1][
        "policy"
    ]
    assert (proposed["id"], proposed["status"], proposed["author"]) == ("v1", "proposed", "Ada")

    assert (
        "second admin"
        in _call(dev_mode, {"action": "review_policy", "version": "v1", "approve": True}, "ada")[1]["error"]
    )
    assert (
        "only an approved version" in _call(dev_mode, {"action": "activate_policy", "version": "v1"}, "ada")[1]["error"]
    )
    assert (
        _call(dev_mode, {"action": "review_policy", "version": "v1", "approve": True}, "bob")[1]["policy"]["status"]
        == "approved"
    )
    assert _call(dev_mode, {"action": "activate_policy", "version": "v1"}, "ada")[1]["policy"]["status"] == "active"

    in_force = _call(dev_mode, {"action": "get_policy"}, "vic")[1]["policy"]
    assert in_force["version"].startswith("v1-") and list(in_force["actions"]) == ["restart_service"]
    audit = _call(dev_mode, {"action": "verify_audit", "incident_id": "policy"}, "jane")[1]
    assert audit["ok"] and audit["events"] == 3


def test_activating_a_new_version_supersedes_the_old(store):
    for n, author, reviewer in ((1, "a", "b"), (2, "b", "a")):
        text = f"version: {n}\nactions:\n  flush_cache: {{risk: low}}\n"
        policy_admin.propose(store, text, author)
        policy_admin.review(store, f"v{n}", reviewer, approve=True)
        policy_admin.activate(store, f"v{n}", author)
    assert [(v["id"], v["status"]) for v in policy_admin.history(store)] == [("v2", "active"), ("v1", "superseded")]
    assert get_policy(store).version.startswith("v2-")
    with pytest.raises(policy_admin.PolicyChangeError, match="Invalid policy"):
        policy_admin.propose(store, "actions: 3", "a")


# OIDC ---------------------------------------------------------------------------------------------


@pytest.fixture
def oidc(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="k1", use="sig", alg="RS256")
    issuer = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Test"
    monkeypatch.setenv("OPSRELAY_AUTH_MODE", "oidc")
    monkeypatch.setenv("OPSRELAY_OIDC_ISSUER", issuer)
    monkeypatch.setenv("OPSRELAY_OIDC_AUDIENCE", "opsrelay-app")
    get_settings.cache_clear()
    auth.clear_caches()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: {"keys": [jwk]})

    def make(**claims):
        now = int(time.time())
        body = {"iss": issuer, "aud": "opsrelay-app", "sub": "u-1", "exp": now + 300, "iat": now, **claims}
        return jwt.encode(body, key, algorithm="RS256", headers={"kid": "k1"})

    yield make
    auth.clear_caches()


def test_oidc_tokens_are_verified(oidc):
    principal = auth.authenticate(
        "Bearer " + oidc(email="jane@corp.example", **{"cognito:groups": ["sre", "not-a-role"]})
    )
    assert (principal.name, principal.roles, principal.method) == ("jane@corp.example", {"sre"}, "oidc")

    for bad in (oidc(exp=int(time.time()) - 10), oidc(aud="someone-else"), oidc(iss="https://evil.example")):
        with pytest.raises(auth.AuthError):
            auth.authenticate("Bearer " + bad)
    with pytest.raises(auth.AuthError):
        auth.authenticate(None)
