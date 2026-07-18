from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from lunar_agent import codex_agent as codex_module
from lunar_agent.models import ExplorerAgentRespondRequest


def _request() -> ExplorerAgentRespondRequest:
    return ExplorerAgentRespondRequest(
        sessionId="explorer-session-1",
        requestId="turn-0001",
        clientId="client-1",
        queryPreview="Current Explorer scope",
        queryContext={},
        querySummary={},
        currentUserMessage="Investigate Acme",
        selectedEntities=[
            {
                "id": "entity-1",
                "type": "company",
                "label": "Acme",
                "graph_ref": "nodes_vertex_collection/acme",
            }
        ],
        conversationHistory=[],
    )


def test_explorer_agent_request_accepts_backend_graph_ref_shape():
    request = _request()

    assert request.selectedEntities[0].graph_ref == "nodes_vertex_collection/acme"


def test_explorer_agent_request_rejects_oversized_message():
    with pytest.raises(ValueError):
        ExplorerAgentRespondRequest(
            sessionId="explorer-session-1",
            requestId="turn-0001",
            clientId="client-1",
            queryPreview="Current Explorer scope",
            currentUserMessage="word " * 1201,
        )


def test_project_root_uses_deployed_application_root(tmp_path, monkeypatch):
    runtime = tmp_path / "codex_runtime"
    runtime.mkdir()
    (runtime / "runner.mjs").write_text("// deployed runner", encoding="utf-8")
    monkeypatch.setenv("LUNAR_AGENT_APP_ROOT", str(tmp_path))

    assert codex_module._project_root() == tmp_path.resolve()
    assert codex_module._runner_path() == runtime / "runner.mjs"


def test_runtime_streams_events_and_keeps_service_secrets_out_of_child_env(
    tmp_path,
    monkeypatch,
):
    runner = tmp_path / "fake-runner.mjs"
    runner.write_text(
        """
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
process.stdout.write(JSON.stringify({
  kind: "event",
  eventType: "tool.progress",
  data: {
    message: "working",
    leakedOpenAi: Boolean(process.env.OPENAI_API_KEY),
    leakedBackend: Boolean(process.env.LUNAR_AGENT_BACKEND_SHARED_TOKEN)
  }
}) + "\\n");
process.stdout.write(JSON.stringify({
  kind: "result",
  codexThreadId: "codex-1",
  model: payload.model,
  finalResponse: "Grounded result",
  actions: [],
  followUps: [],
  entities: [{id: "acme", type: "company", label: "Acme"}],
  citations: []
}) + "\\n");
""".strip(),
        encoding="utf-8",
    )
    mcp = tmp_path / "fake-mcp.mjs"
    mcp.write_text("// exists for runtime validation", encoding="utf-8")
    monkeypatch.setattr(codex_module, "_runner_path", lambda: runner)
    monkeypatch.setattr(codex_module, "_mcp_server_path", lambda: mcp)
    monkeypatch.setattr(codex_module.settings, "codex_agent_workspace_root", str(tmp_path / "work"))
    monkeypatch.setattr(codex_module.settings, "backend_shared_token", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("LUNAR_AGENT_BACKEND_SHARED_TOKEN", "must-not-leak")
    events = []

    async def bootstrap(_request):
        return {
            "token": "delegated-read-only-token",
            "toolsUrl": "https://backend.example.test/api/v1/graph/ai-agent/codex-tools",
        }

    async def sink(event_type, data):
        events.append((event_type, data))

    response = asyncio.run(
        codex_module.run_explorer_codex_turn(
            _request(),
            graph_bootstrap=bootstrap,
            event_sink=sink,
        )
    )

    assert response.reply == "Grounded result"
    assert response.codexThreadId == "codex-1"
    assert response.entities[0]["label"] == "Acme"
    assert events == [
        (
            "tool.progress",
            {
                "message": "working",
                "leakedOpenAi": False,
                "leakedBackend": False,
            },
        )
    ]
    marker = next((tmp_path / "work").glob("*/README.md"))
    assert "isolated workspace" in marker.read_text(encoding="utf-8")


def test_runtime_rejects_incomplete_graph_bootstrap(tmp_path, monkeypatch):
    runner = tmp_path / "fake-runner.mjs"
    runner.write_text("", encoding="utf-8")
    mcp = tmp_path / "fake-mcp.mjs"
    mcp.write_text("", encoding="utf-8")
    monkeypatch.setattr(codex_module, "_runner_path", lambda: runner)
    monkeypatch.setattr(codex_module, "_mcp_server_path", lambda: mcp)

    async def bootstrap(_request):
        return {"token": "", "toolsUrl": ""}

    events = []

    async def sink(event_type, data):
        events.append((event_type, data))

    with pytest.raises(codex_module.ExplorerCodexRuntimeError) as exc_info:
        asyncio.run(
            codex_module.run_explorer_codex_turn(
                _request(),
                graph_bootstrap=bootstrap,
                event_sink=sink,
            )
        )

    assert exc_info.value.code == "graph_bridge_unavailable"
    assert events[0][0] == "tool.progress"
    assert events[0][1]["errorCode"] == "graph_bridge_unavailable"


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, "delegated_token_rejected"),
        (403, "delegated_token_rejected"),
        (409, "graph_session_stale"),
        (429, "graph_tool_rate_limited"),
        (503, "graph_bridge_unavailable"),
    ],
)
def test_runtime_classifies_graph_bootstrap_http_failures(status, expected_code):
    request = httpx.Request("POST", "https://backend.example.test/codex-session")
    response = httpx.Response(status, request=request)
    error = httpx.HTTPStatusError("backend rejected request", request=request, response=response)

    assert codex_module._runtime_failure_code(error) == expected_code


def test_runtime_emits_safe_auth_failure_without_logging_stderr_secret(
    tmp_path,
    monkeypatch,
    caplog,
):
    runner = tmp_path / "failed-runner.mjs"
    runner.write_text(
        """
for await (const _chunk of process.stdin) {}
process.stderr.write("Codex login required; bearer token smoke-super-secret");
process.exit(1);
""".strip(),
        encoding="utf-8",
    )
    mcp = tmp_path / "fake-mcp.mjs"
    mcp.write_text("// exists for runtime validation", encoding="utf-8")
    monkeypatch.setattr(codex_module, "_runner_path", lambda: runner)
    monkeypatch.setattr(codex_module, "_mcp_server_path", lambda: mcp)
    monkeypatch.setattr(codex_module.settings, "codex_agent_workspace_root", str(tmp_path / "work"))
    events = []

    async def bootstrap(_request):
        return {
            "token": "delegated-read-only-token",
            "toolsUrl": "https://backend.example.test/api/v1/graph/ai-agent/codex-tools",
        }

    async def sink(event_type, data):
        events.append((event_type, data))

    with caplog.at_level(logging.INFO):
        with pytest.raises(codex_module.ExplorerCodexRuntimeError) as exc_info:
            asyncio.run(
                codex_module.run_explorer_codex_turn(
                    _request(),
                    graph_bootstrap=bootstrap,
                    event_sink=sink,
                )
            )

    assert exc_info.value.code == "codex_auth_unavailable"
    assert len(events) == 1
    event_type, event = events[0]
    assert event_type == "tool.progress"
    assert event == {
        "phase": "runtime",
        "status": "failed",
        "errorCode": "codex_auth_unavailable",
        "message": (
            "ChatGPT-managed Codex authentication is unavailable. "
            "The service operator must reconnect it."
        ),
        "durationMs": event["durationMs"],
        "retryable": True,
    }
    assert event["durationMs"] >= 0
    assert "smoke-super-secret" not in str(events)
    assert "smoke-super-secret" not in caplog.text
    assert "explorer-session-1" not in caplog.text
    assert "turn-0001" not in caplog.text


def test_graph_bootstrap_failure_emits_safe_non_retryable_stale_session_event(
    monkeypatch,
):
    events = []

    async def bootstrap(_request):
        request = httpx.Request("POST", "https://backend.example.test/codex-session")
        response = httpx.Response(409, request=request)
        raise httpx.HTTPStatusError(
            "Explorer Agent turn is not active",
            request=request,
            response=response,
        )

    async def sink(event_type, data):
        events.append((event_type, data))

    with pytest.raises(codex_module.ExplorerCodexRuntimeError) as exc_info:
        asyncio.run(
            codex_module.run_explorer_codex_turn(
                _request(),
                graph_bootstrap=bootstrap,
                event_sink=sink,
            )
        )

    assert exc_info.value.code == "graph_session_stale"
    assert events[0][0] == "tool.progress"
    assert events[0][1]["errorCode"] == "graph_session_stale"
    assert events[0][1]["retryable"] is False
    assert "fresh turn" in events[0][1]["message"]


def test_runtime_process_launch_failure_emits_safe_retryable_event(
    tmp_path,
    monkeypatch,
):
    runner = tmp_path / "runner.mjs"
    runner.write_text("// present", encoding="utf-8")
    mcp = tmp_path / "mcp.mjs"
    mcp.write_text("// present", encoding="utf-8")
    monkeypatch.setattr(codex_module, "_runner_path", lambda: runner)
    monkeypatch.setattr(codex_module, "_mcp_server_path", lambda: mcp)
    monkeypatch.setattr(codex_module.settings, "codex_agent_workspace_root", str(tmp_path / "work"))

    async def fail_to_launch(*_args, **_kwargs):
        raise FileNotFoundError("node runtime missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_to_launch)
    events = []

    async def bootstrap(_request):
        return {
            "token": "delegated-read-only-token",
            "toolsUrl": "https://backend.example.test/api/v1/graph/ai-agent/codex-tools",
        }

    async def sink(event_type, data):
        events.append((event_type, data))

    with pytest.raises(codex_module.ExplorerCodexRuntimeError) as exc_info:
        asyncio.run(
            codex_module.run_explorer_codex_turn(
                _request(),
                graph_bootstrap=bootstrap,
                event_sink=sink,
            )
        )

    assert exc_info.value.code == "runtime_unavailable"
    assert events[0][0] == "tool.progress"
    assert events[0][1]["errorCode"] == "runtime_unavailable"
    assert events[0][1]["retryable"] is True


def test_runtime_cancellation_terminates_node_process(tmp_path, monkeypatch):
    runner = tmp_path / "waiting-runner.mjs"
    ready = tmp_path / "ready.txt"
    stopped = tmp_path / "stopped.txt"
    runner.write_text(
        f"""
import fs from "node:fs";
fs.writeFileSync({str(ready)!r}, "ready");
process.once("SIGTERM", () => {{
  fs.writeFileSync({str(stopped)!r}, "stopped");
  process.exit(0);
}});
setInterval(() => {{}}, 1000);
""".strip(),
        encoding="utf-8",
    )
    mcp = tmp_path / "fake-mcp.mjs"
    mcp.write_text("// exists for runtime validation", encoding="utf-8")
    monkeypatch.setattr(codex_module, "_runner_path", lambda: runner)
    monkeypatch.setattr(codex_module, "_mcp_server_path", lambda: mcp)
    monkeypatch.setattr(codex_module.settings, "codex_agent_workspace_root", str(tmp_path / "work"))

    async def bootstrap(_request):
        return {
            "token": "delegated-read-only-token",
            "toolsUrl": "https://backend.example.test/api/v1/graph/ai-agent/codex-tools",
        }

    async def scenario():
        task = asyncio.create_task(
            codex_module.run_explorer_codex_turn(_request(), graph_bootstrap=bootstrap)
        )
        for _ in range(100):
            if ready.exists():
                break
            await asyncio.sleep(0.02)
        assert ready.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert stopped.read_text(encoding="utf-8") == "stopped"
