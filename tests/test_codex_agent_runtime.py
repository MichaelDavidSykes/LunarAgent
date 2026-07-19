from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from pathlib import Path

import httpx
import pytest

from lunar_agent import codex_agent as codex_module
from lunar_agent.models import ExplorerAgentRespondRequest


@pytest.fixture(autouse=True)
def configured_command_broker(monkeypatch):
    monkeypatch.setattr(codex_module, "_codex_auth_failure_fingerprint", None)
    monkeypatch.setattr(codex_module, "_codex_auth_probe_cache", None)
    monkeypatch.setattr(
        codex_module.settings,
        "command_broker_socket",
        "/run/lunar-agent-command-broker/broker.sock",
    )
    monkeypatch.setattr(
        codex_module.settings,
        "command_broker_token",
        "broker-test-token",
    )


def test_codex_auth_status_probes_exact_chatgpt_login_method(
    tmp_path,
    monkeypatch,
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    cli = tmp_path / "codex.js"
    cli.write_text(
        'process.stdout.write("Logged in using ChatGPT\\n");',
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_module.settings, "codex_home", str(codex_home))
    monkeypatch.setattr(codex_module.settings, "codex_cli_path", str(cli))

    status = asyncio.run(codex_module.codex_auth_status())

    assert status == {
        "configured": True,
        "mode": "chatgpt",
        "detail": "ChatGPT-managed Codex authentication",
    }


def test_codex_auth_status_rejects_api_key_login(
    tmp_path,
    monkeypatch,
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    cli = tmp_path / "codex.js"
    cli.write_text(
        'process.stdout.write("Logged in using an API key\\n");',
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_module.settings, "codex_home", str(codex_home))
    monkeypatch.setattr(codex_module.settings, "codex_cli_path", str(cli))

    status = asyncio.run(codex_module.codex_auth_status())

    assert status == {
        "configured": False,
        "mode": "chatgpt",
        "detail": "ChatGPT-managed Codex authentication is unavailable",
    }


def test_codex_auth_failure_stays_unready_until_credentials_rotate(
    tmp_path,
    monkeypatch,
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    auth_file = codex_home / "auth.json"
    auth_file.write_text('{"generation":1}', encoding="utf-8")
    cli = tmp_path / "codex.js"
    cli.write_text(
        'process.stdout.write("Logged in using ChatGPT\\n");',
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_module.settings, "codex_home", str(codex_home))
    monkeypatch.setattr(codex_module.settings, "codex_cli_path", str(cli))
    codex_module._mark_codex_auth_unavailable()

    unavailable = asyncio.run(codex_module.codex_auth_status())

    assert unavailable == {
        "configured": False,
        "mode": "chatgpt",
        "detail": "ChatGPT-managed Codex authentication must be reconnected",
    }

    auth_file.write_text('{"generation":2,"rotated":true}', encoding="utf-8")
    recovered = asyncio.run(codex_module.codex_auth_status())

    assert recovered["configured"] is True
    assert recovered["mode"] == "chatgpt"
    assert codex_module._codex_auth_failure_fingerprint is None


def test_codex_auth_probe_is_briefly_cached_and_busted_by_rotation(
    tmp_path,
    monkeypatch,
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    auth_file = codex_home / "auth.json"
    auth_file.write_text('{"generation":1}', encoding="utf-8")
    cli = tmp_path / "codex.js"
    cli.write_text("// present", encoding="utf-8")
    probe_count = 0

    async def chatgpt_login():
        nonlocal probe_count
        probe_count += 1
        return "chatgpt"

    monkeypatch.setattr(codex_module.settings, "codex_home", str(codex_home))
    monkeypatch.setattr(codex_module.settings, "codex_cli_path", str(cli))
    monkeypatch.setattr(codex_module, "_codex_login_method", chatgpt_login)

    assert asyncio.run(codex_module.codex_auth_status())["configured"] is True
    assert asyncio.run(codex_module.codex_auth_status())["configured"] is True
    assert probe_count == 1

    auth_file.write_text('{"generation":2,"rotated":true}', encoding="utf-8")
    assert asyncio.run(codex_module.codex_auth_status())["configured"] is True
    assert probe_count == 2


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


@pytest.mark.parametrize(
    ("payload", "accepted"),
    [
        ({"status": "ok", "sandbox": "bubblewrap"}, False),
        (
            {
                "status": "ok",
                "sandbox": "bubblewrap",
                "verification": "executable",
            },
            True,
        ),
    ],
)
def test_command_broker_status_requires_executable_probe(
    payload,
    accepted,
    monkeypatch,
):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, headers):
            assert url == "http://command-broker/live"
            assert headers == {"Authorization": "Bearer broker-test-token"}
            return Response()

    monkeypatch.setattr(codex_module.Path, "is_socket", lambda _path: True)
    monkeypatch.setattr(
        codex_module.httpx,
        "AsyncHTTPTransport",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(codex_module.httpx, "AsyncClient", Client)

    if accepted:
        result = asyncio.run(codex_module.command_broker_status())
        assert result == {
            "configured": True,
            "mode": "isolated-workspace",
            "verified": True,
        }
    else:
        with pytest.raises(codex_module.ExplorerCodexRuntimeError) as exc_info:
            asyncio.run(codex_module.command_broker_status())
        assert exc_info.value.code == "command_sandbox_unavailable"


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
  kind: "checkpoint",
  checkpointType: "codex_thread",
  codexThreadId: "codex-1"
}) + "\\n");
process.stdout.write(JSON.stringify({
  kind: "event",
  eventType: "tool.progress",
  data: {
    message: "working",
    leakedOpenAi: Boolean(process.env.OPENAI_API_KEY),
    leakedBackend: Boolean(process.env.LUNAR_AGENT_BACKEND_SHARED_TOKEN),
    leakedCommandBroker: Boolean(process.env.LUNAR_AGENT_COMMAND_BROKER_TOKEN),
    commandBrokerConfigured: Boolean(
      payload.commandBrokerSocket &&
      payload.commandBrokerToken &&
      payload.commandWorkspaceId
    )
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
    checkpoints = []

    async def bootstrap(_request):
        return {
            "token": "delegated-read-only-token",
            "toolsUrl": "https://backend.example.test/api/v1/graph/ai-agent/codex-tools",
        }

    async def sink(event_type, data):
        events.append((event_type, data))

    async def checkpoint(codex_thread_id):
        checkpoints.append(codex_thread_id)
        return True

    response = asyncio.run(
        codex_module.run_explorer_codex_turn(
            _request(),
            graph_bootstrap=bootstrap,
            event_sink=sink,
            checkpoint_sink=checkpoint,
        )
    )

    assert response.reply == "Grounded result"
    assert response.codexThreadId == "codex-1"
    assert response.entities[0]["label"] == "Acme"
    assert checkpoints == ["codex-1"]
    assert events == [
        (
            "tool.progress",
            {
                "message": "working",
                "leakedOpenAi": False,
                "leakedBackend": False,
                "leakedCommandBroker": False,
                "commandBrokerConfigured": True,
            },
        )
    ]
    marker = next((tmp_path / "work").glob("*/README.md"))
    assert "isolated workspace" in marker.read_text(encoding="utf-8")


def test_runtime_recovers_the_backend_checkpointed_codex_thread(
    tmp_path,
    monkeypatch,
):
    runner = tmp_path / "checkpoint-runner.mjs"
    runner.write_text(
        """
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
if (payload.codexThreadId !== "codex-durable-1") process.exit(7);
process.stdout.write(JSON.stringify({
  kind: "checkpoint",
  checkpointType: "codex_thread",
  codexThreadId: payload.codexThreadId
}) + "\\n");
process.stdout.write(JSON.stringify({
  kind: "result",
  codexThreadId: payload.codexThreadId,
  model: payload.model,
  finalResponse: "Recovered grounded result",
  actions: [],
  followUps: [],
  entities: [],
  citations: []
}) + "\\n");
""".strip(),
        encoding="utf-8",
    )
    mcp = tmp_path / "fake-mcp.mjs"
    mcp.write_text("// exists", encoding="utf-8")
    monkeypatch.setattr(codex_module, "_runner_path", lambda: runner)
    monkeypatch.setattr(codex_module, "_mcp_server_path", lambda: mcp)
    monkeypatch.setattr(
        codex_module.settings,
        "codex_agent_workspace_root",
        str(tmp_path / "work"),
    )
    checkpoints = []

    async def bootstrap(_request):
        return {
            "token": "delegated-read-only-token",
            "toolsUrl": (
                "https://backend.example.test/"
                "api/v1/graph/ai-agent/codex-tools"
            ),
            "codexThreadId": "codex-durable-1",
        }

    async def checkpoint(codex_thread_id):
        checkpoints.append(codex_thread_id)
        return True

    async def sink(_event_type, _data):
        return None

    response = asyncio.run(codex_module.run_explorer_codex_turn(
        _request(),
        graph_bootstrap=bootstrap,
        event_sink=sink,
        checkpoint_sink=checkpoint,
    ))

    assert response.reply == "Recovered grounded result"
    assert response.codexThreadId == "codex-durable-1"
    assert checkpoints == ["codex-durable-1"]


def test_runtime_rejects_conflicting_backend_thread_checkpoint(
    tmp_path,
    monkeypatch,
):
    runner = tmp_path / "runner.mjs"
    runner.write_text("// must not run", encoding="utf-8")
    mcp = tmp_path / "mcp.mjs"
    mcp.write_text("// exists", encoding="utf-8")
    request = _request().model_copy(
        update={"codexThreadId": "codex-request-thread"}
    )
    monkeypatch.setattr(codex_module, "_runner_path", lambda: runner)
    monkeypatch.setattr(codex_module, "_mcp_server_path", lambda: mcp)
    events = []

    async def bootstrap(_request):
        return {
            "token": "delegated-read-only-token",
            "toolsUrl": (
                "https://backend.example.test/"
                "api/v1/graph/ai-agent/codex-tools"
            ),
            "codexThreadId": "codex-other-thread",
        }

    async def sink(event_type, data):
        events.append((event_type, data))

    with pytest.raises(codex_module.ExplorerCodexRuntimeError) as exc_info:
        asyncio.run(codex_module.run_explorer_codex_turn(
            request,
            graph_bootstrap=bootstrap,
            event_sink=sink,
        ))

    assert exc_info.value.code == "graph_session_stale"
    assert events[-1][1]["errorCode"] == "graph_session_stale"


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


def test_runtime_fails_closed_when_command_broker_is_unconfigured(
    tmp_path,
    monkeypatch,
):
    runner = tmp_path / "fake-runner.mjs"
    runner.write_text("", encoding="utf-8")
    mcp = tmp_path / "fake-mcp.mjs"
    mcp.write_text("", encoding="utf-8")
    monkeypatch.setattr(codex_module, "_runner_path", lambda: runner)
    monkeypatch.setattr(codex_module, "_mcp_server_path", lambda: mcp)
    monkeypatch.setattr(codex_module.settings, "command_broker_token", "")
    events = []

    async def sink(event_type, data):
        events.append((event_type, data))

    with pytest.raises(codex_module.ExplorerCodexRuntimeError) as exc_info:
        asyncio.run(
            codex_module.run_explorer_codex_turn(
                _request(),
                graph_bootstrap=lambda _request: None,
                event_sink=sink,
            )
        )

    assert exc_info.value.code == "command_sandbox_unavailable"
    assert events == [
        (
            "tool.progress",
            {
                "phase": "runtime",
                "status": "failed",
                "errorCode": "command_sandbox_unavailable",
                "message": (
                    "The isolated workspace command service is temporarily unavailable. "
                    "Retry this investigation."
                ),
                "durationMs": 0,
                "retryable": True,
            },
        )
    ]


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
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(codex_module, "_runner_path", lambda: runner)
    monkeypatch.setattr(codex_module, "_mcp_server_path", lambda: mcp)
    monkeypatch.setattr(codex_module.settings, "codex_home", str(codex_home))
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
    assert (
        codex_module._codex_auth_failure_fingerprint
        == codex_module._codex_auth_file_fingerprint()
    )


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


def test_runtime_cancellation_terminates_runner_process_group(tmp_path, monkeypatch):
    runner = tmp_path / "waiting-runner-with-child.mjs"
    ready = tmp_path / "ready.txt"
    runner_stopped = tmp_path / "runner-stopped.txt"
    child_stopped = tmp_path / "child-stopped.txt"
    runner.write_text(
        f"""
import fs from "node:fs";
import {{ spawn }} from "node:child_process";
const childScript = `
  const fs = require("node:fs");
  process.once("SIGTERM", () => {{
    fs.writeFileSync({str(child_stopped)!r}, "stopped");
  }});
  fs.writeFileSync({str(ready)!r}, String(process.pid));
  setInterval(() => {{}}, 1000);
`;
const child = spawn(process.execPath, ["-e", childScript], {{
  stdio: "ignore",
}});
process.once("SIGTERM", () => {{
  fs.writeFileSync({str(runner_stopped)!r}, "stopped");
  setTimeout(() => process.exit(0), 100);
}});
setInterval(() => {{}}, 1000);
""".strip(),
        encoding="utf-8",
    )
    mcp = tmp_path / "fake-mcp.mjs"
    mcp.write_text("// exists for runtime validation", encoding="utf-8")
    monkeypatch.setattr(codex_module, "_runner_path", lambda: runner)
    monkeypatch.setattr(codex_module, "_mcp_server_path", lambda: mcp)
    monkeypatch.setattr(
        codex_module.settings,
        "codex_agent_workspace_root",
        str(tmp_path / "work"),
    )

    async def bootstrap(_request):
        return {
            "token": "delegated-read-only-token",
            "toolsUrl": "https://backend.example.test/api/v1/graph/ai-agent/codex-tools",
        }

    async def scenario():
        task = asyncio.create_task(
            codex_module.run_explorer_codex_turn(
                _request(),
                graph_bootstrap=bootstrap,
            )
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

    assert runner_stopped.read_text(encoding="utf-8") == "stopped"
    assert child_stopped.read_text(encoding="utf-8") == "stopped"
    child_pid = int(ready.read_text(encoding="utf-8"))

    def child_is_running() -> bool:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return False
        proc_stat = Path(f"/proc/{child_pid}/stat")
        if proc_stat.is_file():
            try:
                return proc_stat.read_text(encoding="utf-8").split()[2] != "Z"
            except (OSError, IndexError):
                pass
        return True

    for _ in range(100):
        if not child_is_running():
            break
        time.sleep(0.02)
    else:
        os.kill(child_pid, signal.SIGKILL)
        pytest.fail("Runner child survived cancellation")
