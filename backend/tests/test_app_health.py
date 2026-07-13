import inspect

from fastapi.testclient import TestClient

import app


def test_health_returns_ok():
    client = TestClient(app.app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_handler_is_async():
    # Must be async so it runs on the event loop, not the threadpool that
    # long crawls can saturate (ALB health-check starvation).
    assert inspect.iscoroutinefunction(app.health)
