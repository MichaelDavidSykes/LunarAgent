import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from lunar_agent import main as main_module


def _authenticated_client(monkeypatch):
    monkeypatch.setattr(main_module.settings, "shared_token", "unit-test-shared-token")
    client = TestClient(main_module.app)
    client.headers.update({"Authorization": "Bearer unit-test-shared-token"})
    return client


def test_explorer_agent_endpoint_hides_internal_error_detail(monkeypatch):
    async def fail_respond(_request):
        raise RuntimeError("openai-provider-secret-token")

    monkeypatch.setattr(main_module, "run_explorer_codex_turn", fail_respond)
    client = _authenticated_client(monkeypatch)

    response = client.post(
        "/v1/explorer-agent/respond",
        json={
            "sessionId": "session-1",
            "requestId": "turn-0001",
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
    async def fail_research_safe_route_area_risk(**_kwargs):
        raise RuntimeError("backend-shared-token")

    monkeypatch.setattr(main_module, "research_safe_route_area_risk", fail_research_safe_route_area_risk)
    client = _authenticated_client(monkeypatch)

    response = client.post(
        "/v1/safe-route/area-risk/research",
        json={
            "sessionId": "session-1",
            "requestId": "turn-0001",
            "aoi": {"bounds": {"minLat": 0, "minLon": 0, "maxLat": 1, "maxLon": 1}},
            "evidence": [],
            "maxZones": 8,
        },
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "Area risk research failed."}
    assert "backend-shared-token" not in response.text


def test_threatscape_query_risk_alias_uses_area_risk_research(monkeypatch):
    async def fake_research_safe_route_area_risk(**kwargs):
        assert "session_id" not in kwargs
        return {"zones": [{"label": "Johannesburg"}], "model": "test-model", "notes": "ok"}

    monkeypatch.setattr(main_module, "research_safe_route_area_risk", fake_research_safe_route_area_risk)
    client = _authenticated_client(monkeypatch)

    response = client.post(
        "/v1/threatscape/query-risk/research",
        json={
            "sessionId": "session-1",
            "aoi": {"bounds": {"minLat": 0, "minLon": 0, "maxLat": 1, "maxLon": 1}},
            "evidence": [],
            "maxZones": 8,
        },
    )

    assert response.status_code == 200
    assert response.json()["zones"] == [{"label": "Johannesburg"}]


def test_agent_routes_fail_closed_when_shared_token_is_unconfigured(monkeypatch):
    monkeypatch.setattr(main_module.settings, "shared_token", "")
    client = TestClient(main_module.app)

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

    assert response.status_code == 503
    assert response.json() == {"detail": "Agent authentication is unavailable"}
    assert client.get("/health").status_code == 503


def test_agent_rejects_invalid_bearer_token(monkeypatch):
    monkeypatch.setattr(main_module.settings, "shared_token", "configured-secret")
    client = TestClient(main_module.app)

    response = client.post(
        "/v1/explorer-agent/respond",
        headers={"Authorization": "Bearer wrong-secret"},
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

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"

    with pytest.raises(HTTPException) as unicode_error:
        main_module.require_token("Bearer attacker-💥")
    assert unicode_error.value.status_code == 401


def test_health_reports_authentication_is_configured(monkeypatch):
    monkeypatch.setattr(main_module.settings, "shared_token", "configured-secret")
    monkeypatch.setattr(main_module.settings, "openai_api_key", "configured-openai-key")
    monkeypatch.setattr(main_module.settings, "backend_base_url", "https://backend.example.test")
    monkeypatch.setattr(main_module.settings, "backend_shared_token", "configured-backend-token")
    response = TestClient(main_module.app).get("/health")

    assert response.status_code == 200
    assert response.json()["authConfigured"] is True
    assert response.json()["dependenciesConfigured"] is True


def test_health_fails_when_runtime_dependencies_are_missing(monkeypatch):
    monkeypatch.setattr(main_module.settings, "shared_token", "configured-secret")
    monkeypatch.setattr(main_module.settings, "openai_api_key", "")
    monkeypatch.setattr(main_module.settings, "backend_base_url", "")
    monkeypatch.setattr(main_module.settings, "backend_shared_token", "")

    assert TestClient(main_module.app).get("/health").status_code == 503
    assert TestClient(main_module.app).get("/live").status_code == 200


def test_explorer_agent_endpoint_uses_codex_runtime(monkeypatch):
    async def fake_codex_turn(request):
        assert request.sessionId == "explorer-session-1"
        assert request.requestId == "turn-0001"
        return {
            "reply": "Investigated",
            "codexThreadId": "codex-thread-1",
            "model": "gpt-5.6-sol",
            "actions": [],
            "followUps": [],
            "entities": [],
            "citations": [],
        }

    monkeypatch.setattr(main_module, "run_explorer_codex_turn", fake_codex_turn)
    client = _authenticated_client(monkeypatch)
    response = client.post(
        "/v1/explorer-agent/respond",
        json={
            "sessionId": "explorer-session-1",
            "requestId": "turn-0001",
            "clientId": "client-1",
            "queryPreview": "Current Explorer scope",
            "queryContext": {},
            "querySummary": {},
            "currentUserMessage": "Investigate Acme",
            "selectedEntities": [],
            "conversationHistory": [],
        },
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "Investigated"
    assert response.json()["model"] == "gpt-5.6-sol"


def test_explorer_agent_endpoint_hides_codex_failure_detail(monkeypatch):
    async def fail_codex_turn(_request):
        raise RuntimeError("chatgpt-auth-secret")

    monkeypatch.setattr(main_module, "run_explorer_codex_turn", fail_codex_turn)
    client = _authenticated_client(monkeypatch)
    response = client.post(
        "/v1/explorer-agent/respond",
        json={
            "sessionId": "explorer-session-1",
            "requestId": "turn-0001",
            "clientId": "client-1",
            "queryPreview": "Current Explorer scope",
            "queryContext": {},
            "querySummary": {},
            "currentUserMessage": "Investigate Acme",
        },
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "Explorer agent response failed."}
    assert "chatgpt-auth-secret" not in response.text
