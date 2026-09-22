from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root_returns_hello_world():
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, str) and "Hello, World!" in body
