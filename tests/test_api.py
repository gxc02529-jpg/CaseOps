from fastapi.testclient import TestClient

from caseops.api import create_app


def test_health_and_required_scope_headers() -> None:
    client = TestClient(create_app())
    assert client.get("/health").json() == {"status": "ok"}
    response = client.post(
        "/v1/tickets",
        json={"requester_id": "u1", "subject": "x", "description": "y"},
    )
    assert response.status_code == 422

