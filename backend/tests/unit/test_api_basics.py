"""API behavior that does not need a database: liveness, envelope, request IDs, CORS."""

from fastapi.testclient import TestClient


def test_health_is_live_without_database(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_openapi_documents_endpoints(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths["/api/repositories"]
    assert "get" in paths["/api/repositories"]
    assert "get" in paths["/api/repositories/{repository_id}"]


def test_invalid_url_uses_error_envelope(client: TestClient) -> None:
    r = client.post("/api/repositories", json={"url": "https://evil.com/a/b"})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "invalid_github_url"
    assert err["details"]["reason"] == "host must be github.com"
    assert err["request_id"] == r.headers["x-request-id"]


def test_missing_body_field_is_validation_error(client: TestClient) -> None:
    r = client.post("/api/repositories", json={})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert err["details"]["errors"][0]["loc"] == ["body", "url"]


def test_validation_error_does_not_echo_input(client: TestClient) -> None:
    secret = "ghp_" + "x" * 30
    r = client.post("/api/repositories", json={"url": [secret]})
    assert r.status_code == 422
    assert secret not in r.text


def test_unknown_route_uses_error_envelope(client: TestClient) -> None:
    r = client.get("/api/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_bad_uuid_is_validation_error(client: TestClient) -> None:
    r = client.get("/api/repositories/not-a-uuid")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_client_request_id_is_echoed_when_safe(client: TestClient) -> None:
    r = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"


def test_unsafe_request_id_is_replaced(client: TestClient) -> None:
    r = client.get("/health", headers={"X-Request-ID": "bad id\twith spaces"})
    assert r.headers["x-request-id"] != "bad id\twith spaces"
    assert len(r.headers["x-request-id"]) == 32


def test_cors_allows_configured_origin_only(client: TestClient) -> None:
    ok = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"
    bad = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers


def test_cors_preflight_restricts_methods(client: TestClient) -> None:
    r = client.options(
        "/api/repositories",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "DELETE",
        },
    )
    assert r.status_code == 400  # DELETE is not an allowed method
