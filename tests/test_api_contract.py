from fastapi.testclient import TestClient

from lunar_agent import main as main_module


def _client_without_shared_token(monkeypatch):
    monkeypatch.setattr(main_module.settings, "shared_token", "")
    return TestClient(main_module.app)


def test_explorer_agent_endpoint_hides_internal_error_detail(monkeypatch):
    async def fail_respond(**kwargs):
        raise RuntimeError("openai-provider-secret-token")

    monkeypatch.setattr(main_module, "respond", fail_respond)
    client = _client_without_shared_token(monkeypatch)

    response = client.post(
        "/v1/explorer-agent/respond",
        json={
            "sessionId": "session-1",
            "allowUiActions": False,
            "conversationHistory": [],
            "queryPreview": "FOR doc IN reports RETURN doc",
            "queryContext": {},
            "querySummary": {},
            "currentUserMessage": "What matters?",
        },
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "Explorer agent response failed."}
    assert "openai-provider-secret-token" not in response.text


def test_area_risk_endpoint_hides_internal_error_detail(monkeypatch):
    async def fail_research_safe_route_area_risk(**kwargs):
        raise RuntimeError("backend-shared-token")

    monkeypatch.setattr(main_module, "research_safe_route_area_risk", fail_research_safe_route_area_risk)
    client = _client_without_shared_token(monkeypatch)

    response = client.post(
        "/v1/safe-route/area-risk/research",
        json={
            "sessionId": "session-1",
            "aoi": {"bounds": {"minLat": 0, "minLon": 0, "maxLat": 1, "maxLon": 1}},
            "evidence": [],
            "maxZones": 8,
        },
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "Area risk research failed."}
    assert "backend-shared-token" not in response.text
