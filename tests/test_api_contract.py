import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from lunar_agent import main as main_module
from lunar_agent.models import ExplorerAgentCancelRequest, ExplorerAgentRespondRequest
from lunar_agent.turn_registry import ExplorerTurnRegistry


@pytest.fixture(autouse=True)
def _fresh_explorer_turn_registry(monkeypatch):
    monkeypatch.setattr(main_module, "_explorer_turn_registry", ExplorerTurnRegistry())


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


def test_explorer_health_requires_verified_read_only_policy(monkeypatch):
    async def configured_auth():
        return {"configured": True, "mode": "chatgpt"}

    def unavailable_execution_policy():
        raise RuntimeError("policy-private-detail")

    monkeypatch.setattr(main_module.settings, "shared_token", "configured-secret")
    monkeypatch.setattr(main_module.settings, "codex_agent_enabled", True)
    monkeypatch.setattr(main_module.settings, "backend_base_url", "https://backend.example.test")
    monkeypatch.setattr(main_module.settings, "backend_shared_token", "configured-backend-token")
    monkeypatch.setattr(main_module, "codex_auth_status", configured_auth)
    monkeypatch.setattr(main_module, "execution_policy_status", unavailable_execution_policy)
    client = TestClient(main_module.app)

    response = client.get(
        "/v1/explorer-agent/health",
        headers={"Authorization": "Bearer configured-secret"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "LunarAgent read-only execution policy is unavailable"
    }
    assert "policy-private-detail" not in response.text


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


def test_explorer_agent_endpoint_replays_one_exact_runtime_result(monkeypatch):
    calls = 0

    async def fake_codex_turn(_request):
        nonlocal calls
        calls += 1
        return {
            "reply": "One exact result",
            "model": "gpt-5.6-sol",
            "actions": [],
            "followUps": [],
            "entities": [],
            "citations": [],
        }

    monkeypatch.setattr(main_module, "run_explorer_codex_turn", fake_codex_turn)
    client = _authenticated_client(monkeypatch)
    payload = {
        "sessionId": "explorer-session-replay",
        "requestId": "turn-replay",
        "clientId": "client-1",
        "queryPreview": "Current Explorer scope",
        "queryContext": {},
        "querySummary": {},
        "currentUserMessage": "Investigate Acme",
        "selectedEntities": [],
        "conversationHistory": [],
    }

    first = client.post("/v1/explorer-agent/respond", json=payload)
    replay = client.post("/v1/explorer-agent/respond", json=payload)
    refreshed_snapshot_replay = client.post(
        "/v1/explorer-agent/respond",
        json={**payload, "querySummary": {"reportCount": 99}},
    )
    checkpointed_thread_replay = client.post(
        "/v1/explorer-agent/respond",
        json={**payload, "codexThreadId": "codex-durable-1"},
    )
    conflict = client.post(
        "/v1/explorer-agent/respond",
        json={**payload, "currentUserMessage": "Different input"},
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert refreshed_snapshot_replay.status_code == 200
    assert refreshed_snapshot_replay.json() == first.json()
    assert checkpointed_thread_replay.status_code == 200
    assert checkpointed_thread_replay.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json() == {
        "detail": "Explorer agent turn identity conflict.",
    }
    assert calls == 1


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


def test_explorer_agent_cancel_requires_authentication(monkeypatch):
    monkeypatch.setattr(main_module.settings, "shared_token", "configured-secret")
    response = TestClient(main_module.app).post(
        "/v1/explorer-agent/cancel",
        json={"sessionId": "session-1", "requestId": "request-1"},
    )

    assert response.status_code == 401


def test_explorer_agent_cancel_fences_a_late_response(monkeypatch):
    monkeypatch.setattr(main_module, "_explorer_turn_registry", ExplorerTurnRegistry())
    called = False

    async def fake_codex_turn(_request):
        nonlocal called
        called = True
        return {
            "reply": "must not run",
            "actions": [],
            "followUps": [],
            "entities": [],
            "citations": [],
        }

    monkeypatch.setattr(main_module, "run_explorer_codex_turn", fake_codex_turn)
    client = _authenticated_client(monkeypatch)
    cancel = client.post(
        "/v1/explorer-agent/cancel",
        json={"sessionId": "session-late", "requestId": "request-late"},
    )
    response = client.post(
        "/v1/explorer-agent/respond",
        json={
            "sessionId": "session-late",
            "requestId": "request-late",
            "queryPreview": "Current Explorer scope",
            "queryContext": {},
            "querySummary": {},
            "currentUserMessage": "Investigate Acme",
        },
    )

    assert cancel.status_code == 200
    assert cancel.json()["cancelled"] is False
    assert response.status_code == 409
    assert response.json() == {"detail": "Explorer agent turn was cancelled."}
    assert called is False


def test_explorer_agent_cancel_interrupts_an_active_response(monkeypatch):
    registry = ExplorerTurnRegistry()
    monkeypatch.setattr(main_module, "_explorer_turn_registry", registry)

    async def exercise():
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def long_codex_turn(_request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        monkeypatch.setattr(main_module, "run_explorer_codex_turn", long_codex_turn)
        request = ExplorerAgentRespondRequest(
            sessionId="session-active",
            requestId="request-active",
            queryPreview="Current Explorer scope",
            queryContext={},
            querySummary={},
            currentUserMessage="Investigate Acme",
        )
        response_task = asyncio.create_task(main_module.explorer_agent_respond(request))
        await started.wait()

        result = await main_module.explorer_agent_cancel(ExplorerAgentCancelRequest(
            sessionId="session-active",
            requestId="request-active",
        ))

        assert result.cancelled is True
        with pytest.raises(HTTPException) as error:
            await response_task
        assert error.value.status_code == 409
        assert stopped.is_set()

    asyncio.run(exercise())
