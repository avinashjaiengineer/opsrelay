"""Behind API Gateway or CloudFront: only requests carrying the origin secret get in."""

from starlette.testclient import TestClient

from opsrelay.config import get_settings


def test_requests_must_carry_the_origin_secret(monkeypatch, service):
    from opsrelay.runtime import coordinator

    monkeypatch.setattr(coordinator, "_service", service)
    client = TestClient(coordinator.app)
    assert client.get("/ping").status_code == 200  # no secret configured: open as before

    monkeypatch.setenv("OPSRELAY_ORIGIN_SECRET", "s3cret")
    get_settings.cache_clear()
    assert client.get("/ping").status_code == 403
    assert client.post("/invocations", json={"action": "health"}).status_code == 403
    assert client.get("/ping", headers={"x-opsrelay-origin": "wrong"}).status_code == 403
    ok = client.post("/invocations", json={"action": "health"}, headers={"x-opsrelay-origin": "s3cret"})
    assert ok.status_code == 200 and ok.json()["services"]
