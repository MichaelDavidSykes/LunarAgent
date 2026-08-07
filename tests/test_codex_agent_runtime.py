from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from lunar_agent import codex_agent as codex_module
from lunar_agent.models import (
    ExplorerAgentRespondRequest,
    ExplorerAgentRespondResponse,
)


@pytest.fixture(autouse=True)
def reset_codex_auth_probe(monkeypatch):
    monkeypatch.setattr(codex_module, "_codex_auth_failure_fingerprint", None)
    monkeypatch.setattr(codex_module, "_codex_auth_failure_retry_at", 0.0)
    monkeypatch.setattr(codex_module, "_codex_auth_probe_cache", None)


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


def test_codex_auth_failure_stays_unready_during_recheck_cooldown(
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


def test_codex_auth_failure_recovers_after_bounded_recheck(
    tmp_path,
    monkeypatch,
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        '{"generation":1}',
        encoding="utf-8",
    )
    cli = tmp_path / "codex.js"
    cli.write_text(
        'process.stdout.write("Logged in using ChatGPT\\n");',
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_module.settings, "codex_home", str(codex_home))
    monkeypatch.setattr(codex_module.settings, "codex_cli_path", str(cli))
    codex_module._mark_codex_auth_unavailable()
    monkeypatch.setattr(codex_module, "_codex_auth_failure_retry_at", 0.0)

    recovered = asyncio.run(codex_module.codex_auth_status())

    assert recovered == {
        "configured": True,
        "mode": "chatgpt",
        "detail": "ChatGPT-managed Codex authentication",
    }
    assert codex_module._codex_auth_failure_fingerprint is None


def test_codex_auth_failure_extends_cooldown_when_recheck_fails(
    tmp_path,
    monkeypatch,
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        '{"generation":1}',
        encoding="utf-8",
    )
    cli = tmp_path / "codex.js"
    cli.write_text(
        'process.stdout.write("Not logged in\\n");',
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_module.settings, "codex_home", str(codex_home))
    monkeypatch.setattr(codex_module.settings, "codex_cli_path", str(cli))
    codex_module._mark_codex_auth_unavailable()
    monkeypatch.setattr(codex_module, "_codex_auth_failure_retry_at", 0.0)

    unavailable = asyncio.run(codex_module.codex_auth_status())

    assert unavailable == {
        "configured": False,
        "mode": "chatgpt",
        "detail": "ChatGPT-managed Codex authentication must be reconnected",
    }
    assert codex_module._codex_auth_failure_retry_at > time.monotonic()


def test_codex_auth_failure_recovers_immediately_when_credentials_rotate(
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


def test_area_risk_codex_analysis_uses_chatgpt_auth_without_api_key(
    tmp_path,
    monkeypatch,
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    runner = tmp_path / "area-risk-runner.mjs"
    runner.write_text(
        """
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
if (process.env.OPENAI_API_KEY || process.env.LUNAR_AGENT_SHARED_TOKEN) process.exit(7);
process.stdout.write(JSON.stringify({
  model: payload.model,
  notes: "account provider",
  zones: [{label: "Brixton", evidence_urls: ["https://example.test/source"]}]
}));
""",
        encoding="utf-8",
    )

    async def chatgpt_login(_fingerprint):
        return "chatgpt"

    monkeypatch.setattr(codex_module.settings, "codex_home", str(codex_home))
    monkeypatch.setattr(codex_module.settings, "codex_agent_enabled", True)
    monkeypatch.setattr(codex_module.settings, "area_risk_account_enabled", True)
    monkeypatch.setattr(codex_module.settings, "area_risk_codex_model", "gpt-5.6-sol")
    monkeypatch.setattr(codex_module.settings, "area_risk_codex_reasoning_effort", "low")
    monkeypatch.setattr(codex_module.settings, "area_risk_codex_timeout", 30)
    monkeypatch.setattr(
        codex_module.settings,
        "codex_agent_workspace_root",
        str(tmp_path / "work"),
    )
    monkeypatch.setattr(codex_module, "_area_risk_runner_path", lambda: runner)
    monkeypatch.setattr(codex_module, "_cached_codex_login_method", chatgpt_login)

    result = asyncio.run(
        codex_module.run_area_risk_codex_analysis(
            "Analyze this bounded public evidence.",
            max_zones=3,
            evidence_urls={"https://example.test/source"},
        )
    )

    assert result == {
        "model": "gpt-5.6-sol",
        "notes": "account provider",
        "verifiedSourceUrls": [],
        "webSearchCompleted": False,
        "zones": [
            {
                "label": "Brixton",
                "evidence_urls": ["https://example.test/source"],
            }
        ],
    }
    assert (tmp_path / "work" / "safe-route-area-risk" / "README.md").is_file()


def test_area_risk_codex_analysis_rejects_non_chatgpt_auth(
    tmp_path,
    monkeypatch,
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    runner = tmp_path / "area-risk-runner.mjs"
    runner.write_text("process.exit(0);", encoding="utf-8")

    async def api_login(_fingerprint):
        return "api"

    monkeypatch.setattr(codex_module.settings, "codex_home", str(codex_home))
    monkeypatch.setattr(codex_module.settings, "codex_agent_enabled", True)
    monkeypatch.setattr(codex_module.settings, "area_risk_account_enabled", True)
    monkeypatch.setattr(codex_module, "_area_risk_runner_path", lambda: runner)
    monkeypatch.setattr(codex_module, "_cached_codex_login_method", api_login)

    with pytest.raises(codex_module.ExplorerCodexRuntimeError) as exc_info:
        asyncio.run(
            codex_module.run_area_risk_codex_analysis(
                "Analyze bounded evidence.",
                max_zones=2,
            )
        )

    assert exc_info.value.code == "codex_auth_unavailable"


def _request() -> ExplorerAgentRespondRequest:
    return ExplorerAgentRespondRequest(
        sessionId="explorer-session-1",
        requestId="turn-0001",
        clientId="client-1",
        queryPreview="Current Explorer scope",
        queryContext={},
        querySummary={},
        investigationKnowledge={
            "schemaVersion": 1,
            "graphEntities": [
                {
                    "id": "nodes_vertex_collection/acme",
                    "label": "Acme",
                    "type": "company",
                }
            ],
            "graphSources": [],
        },
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


def test_execution_policy_status_proves_no_host_execution():
    assert codex_module.execution_policy_status() == {
        "configured": True,
        "mode": "read-only-no-host-exec",
        "verified": True,
        "sandboxMode": "read-only",
        "networkAccessEnabled": False,
        "hostCommands": False,
        "fileWrites": False,
    }


def test_execution_policy_status_rejects_policy_drift(tmp_path, monkeypatch):
    runtime = tmp_path / "codex_runtime"
    runtime.mkdir()
    (runtime / "execution_policy.json").write_text(
        '{"schema":1,"mode":"workspace-write"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_module, "_project_root", lambda: tmp_path)

    with pytest.raises(codex_module.ExplorerCodexRuntimeError) as exc_info:
        codex_module.execution_policy_status()

    assert exc_info.value.code == "runtime_unavailable"


def test_project_root_uses_deployed_application_root(tmp_path, monkeypatch):
    runtime = tmp_path / "codex_runtime"
    runtime.mkdir()
    (runtime / "runner.mjs").write_text("// deployed runner", encoding="utf-8")
    monkeypatch.setenv("LUNAR_AGENT_APP_ROOT", str(tmp_path))

    assert codex_module._project_root() == tmp_path.resolve()
    assert codex_module._runner_path() == runtime / "runner.mjs"


def test_safe_runner_env_keeps_windows_runtime_root_without_service_secrets(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("LUNAR_AGENT_BACKEND_SHARED_TOKEN", "must-not-leak")

    environment = codex_module._safe_runner_env()

    if os.name == "nt":
        assert environment["SystemRoot"] == r"C:\Windows"
    else:
        assert "SystemRoot" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "LUNAR_AGENT_BACKEND_SHARED_TOKEN" not in environment


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
    knowledgeGraphRef: payload.investigationKnowledge.graphEntities[0].id,
    hasCommandBrokerPayload: Object.keys(payload).some((key) =>
      key.startsWith("commandBroker") || key === "commandWorkspaceId"
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
  citations: [],
  turnKnowledge: {
    schemaVersion: 1,
    graphEntities: [{
      id: "nodes_vertex_collection/acme",
      label: "Acme",
      type: "company"
    }],
    graphSources: [{
      title: "Acme report",
      url: "https://example.test/acme",
      sourceName: "Example",
      publishedAt: "2026-08-07T00:00:00Z",
      snippet: null
    }]
  }
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
    result_checkpoints = []

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

    async def checkpoint_result(response):
        result_checkpoints.append(response)
        return True

    response = asyncio.run(
        codex_module.run_explorer_codex_turn(
            _request(),
            graph_bootstrap=bootstrap,
            event_sink=sink,
            checkpoint_sink=checkpoint,
            result_checkpoint_sink=checkpoint_result,
        )
    )

    assert response.reply == "Grounded result"
    assert response.codexThreadId == "codex-1"
    assert response.entities[0]["label"] == "Acme"
    assert response.turnKnowledge["schemaVersion"] == 1
    assert response.turnKnowledge["graphEntities"][0]["id"] == (
        "nodes_vertex_collection/acme"
    )
    assert checkpoints == ["codex-1"]
    assert len(result_checkpoints) == 1
    assert result_checkpoints[0].reply == "Grounded result"
    assert result_checkpoints[0].codexThreadId == "codex-1"
    assert result_checkpoints[0].turnKnowledge == response.turnKnowledge
    assert events == [
        (
            "tool.progress",
            {
                "message": "working",
                "leakedOpenAi": False,
                "leakedBackend": False,
                "knowledgeGraphRef": "nodes_vertex_collection/acme",
                "hasCommandBrokerPayload": False,
            },
        )
    ]
    marker = next((tmp_path / "work").glob("*/README.md"))
    assert "read-only to the model" in marker.read_text(encoding="utf-8")


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
    result_checkpoints = []

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

    async def checkpoint_result(response):
        result_checkpoints.append(response)
        return True

    async def sink(_event_type, _data):
        return None

    response = asyncio.run(codex_module.run_explorer_codex_turn(
        _request(),
        graph_bootstrap=bootstrap,
        event_sink=sink,
        checkpoint_sink=checkpoint,
        result_checkpoint_sink=checkpoint_result,
    ))

    assert response.reply == "Recovered grounded result"
    assert response.codexThreadId == "codex-durable-1"
    assert checkpoints == ["codex-durable-1"]
    assert len(result_checkpoints) == 1
    assert result_checkpoints[0].reply == "Recovered grounded result"


def test_runtime_replays_a_verified_durable_result_without_launching_codex(
    tmp_path,
    monkeypatch,
):
    runner = tmp_path / "runner-must-not-launch.mjs"
    runner.write_text("// present but must not launch", encoding="utf-8")
    mcp = tmp_path / "mcp.mjs"
    mcp.write_text("// present", encoding="utf-8")
    monkeypatch.setattr(codex_module, "_runner_path", lambda: runner)
    monkeypatch.setattr(codex_module, "_mcp_server_path", lambda: mcp)

    response = ExplorerAgentRespondResponse(
        reply="Recovered without repeating research",
        actions=[],
        followUps=["Inspect Acme"],
        model="gpt-5.6-sol",
        codexThreadId="codex-durable-1",
        entities=[{"id": "company/acme", "label": "Acme"}],
        citations=[],
    )

    async def bootstrap(_request):
        return {
            "token": "delegated-read-only-token",
            "toolsUrl": (
                "https://backend.example.test/"
                "api/v1/graph/ai-agent/codex-tools"
            ),
            "codexThreadId": "codex-durable-1",
            "resultCheckpoint": {
                "requestId": "turn-0001",
                "codexThreadId": "codex-durable-1",
                "resultFingerprint": (
                    codex_module._runtime_result_fingerprint(response)
                ),
                "response": response.model_dump(
                    mode="json",
                    exclude_none=False,
                ),
            },
        }

    async def fail_process_launch(*_args, **_kwargs):
        raise AssertionError("durable result replay launched a new process")

    async def fail_thread_checkpoint(_codex_thread_id):
        raise AssertionError("durable result replay rewrote thread checkpoint")

    async def fail_result_checkpoint(_response):
        raise AssertionError("durable result replay rewrote result checkpoint")

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        fail_process_launch,
    )

    replayed = asyncio.run(codex_module.run_explorer_codex_turn(
        _request(),
        graph_bootstrap=bootstrap,
        checkpoint_sink=fail_thread_checkpoint,
        result_checkpoint_sink=fail_result_checkpoint,
    ))

    assert replayed == response


def test_runtime_rejects_a_tampered_durable_result_checkpoint(
    tmp_path,
    monkeypatch,
):
    runner = tmp_path / "runner-must-not-launch.mjs"
    runner.write_text("// present but must not launch", encoding="utf-8")
    mcp = tmp_path / "mcp.mjs"
    mcp.write_text("// present", encoding="utf-8")
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
            "codexThreadId": "codex-durable-1",
            "resultCheckpoint": {
                "requestId": "turn-0001",
                "codexThreadId": "codex-durable-1",
                "resultFingerprint": "a" * 64,
                "response": {
                    "reply": "Tampered result",
                    "actions": [],
                    "followUps": [],
                    "model": "gpt-5.6-sol",
                    "codexThreadId": "codex-durable-1",
                    "entities": [],
                    "citations": [],
                },
            },
        }

    async def sink(event_type, data):
        events.append((event_type, data))

    with pytest.raises(codex_module.ExplorerCodexRuntimeError) as exc_info:
        asyncio.run(codex_module.run_explorer_codex_turn(
            _request(),
            graph_bootstrap=bootstrap,
            event_sink=sink,
        ))

    assert exc_info.value.code == "graph_session_stale"
    assert events[-1][1]["errorCode"] == "graph_session_stale"


def test_runtime_result_checkpoint_transport_retries_exact_payload(
    monkeypatch,
):
    response = ExplorerAgentRespondResponse(
        reply="Grounded result",
        model="gpt-5.6-sol",
        codexThreadId="codex-durable-1",
        turnKnowledge={
            "schemaVersion": 1,
            "graphEntities": [
                {
                    "id": "nodes_vertex_collection/acme",
                    "label": "Acme",
                    "type": "company",
                }
            ],
            "graphSources": [],
        },
    )
    calls = []
    delays = []

    async def post_backend(path, payload, timeout_seconds):
        calls.append((path, payload, timeout_seconds))
        if len(calls) < 3:
            request = httpx.Request(
                "POST",
                "https://backend.example.test/runtime-result-checkpoint",
            )
            raise httpx.ConnectError(
                "temporary transport failure",
                request=request,
            )
        return {"accepted": True, "status": "generating"}

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(codex_module, "_post_backend", post_backend)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    accepted = asyncio.run(
        codex_module._forward_runtime_result_checkpoint(
            _request(),
            response,
        )
    )

    assert accepted is True
    assert len(calls) == 3
    assert delays == [0.25, 1.0]
    assert all(
        call[0]
        == "/api/v1/graph/ai-agent/tools/runtime-result-checkpoint"
        for call in calls
    )
    assert all(call[1] == calls[0][1] for call in calls)
    assert calls[0][1]["request_id"] == "turn-0001"
    assert calls[0][1]["codex_thread_id"] == "codex-durable-1"
    assert calls[0][1]["result"]["reply"] == "Grounded result"
    assert calls[0][1]["result"]["turnKnowledge"]["graphEntities"][0]["id"] == (
        "nodes_vertex_collection/acme"
    )


def test_runtime_fails_closed_when_backend_rejects_final_result_checkpoint(
    tmp_path,
    monkeypatch,
):
    runner = tmp_path / "result-runner.mjs"
    runner.write_text(
        """
for await (const _chunk of process.stdin) {}
process.stdout.write(JSON.stringify({
  kind: "result",
  codexThreadId: "codex-1",
  model: "gpt-5.6-sol",
  finalResponse: "Grounded result",
  actions: [],
  followUps: [],
  entities: [],
  citations: []
}) + "\\n");
""".strip(),
        encoding="utf-8",
    )
    mcp = tmp_path / "mcp.mjs"
    mcp.write_text("// present", encoding="utf-8")
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
            "toolsUrl": (
                "https://backend.example.test/"
                "api/v1/graph/ai-agent/codex-tools"
            ),
        }

    async def reject_result(_response):
        return False

    with pytest.raises(codex_module.ExplorerCodexRuntimeError) as exc_info:
        asyncio.run(codex_module.run_explorer_codex_turn(
            _request(),
            graph_bootstrap=bootstrap,
            result_checkpoint_sink=reject_result,
        ))

    assert exc_info.value.code == "graph_session_stale"


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


def test_runtime_fails_closed_when_execution_policy_is_missing(
    tmp_path,
    monkeypatch,
):
    runner = tmp_path / "fake-runner.mjs"
    runner.write_text("", encoding="utf-8")
    mcp = tmp_path / "fake-mcp.mjs"
    mcp.write_text("", encoding="utf-8")
    monkeypatch.setattr(codex_module, "_runner_path", lambda: runner)
    monkeypatch.setattr(codex_module, "_mcp_server_path", lambda: mcp)
    monkeypatch.setattr(codex_module, "_project_root", lambda: tmp_path)
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

    assert exc_info.value.code == "runtime_unavailable"
    assert events == [
        (
            "tool.progress",
            {
                "phase": "runtime",
                "status": "failed",
                "errorCode": "runtime_unavailable",
                "message": "The Codex investigation runtime is temporarily unavailable. Retry this turn.",
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
const shutdownSignal = process.platform === "win32" ? "SIGBREAK" : "SIGTERM";
process.once(shutdownSignal, () => {{
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
  const shutdownSignal = process.platform === "win32" ? "SIGBREAK" : "SIGTERM";
  process.once(shutdownSignal, () => {{
    fs.writeFileSync(process.argv[1], "stopped");
    process.exit(0);
  }});
  fs.writeFileSync(process.argv[2], String(process.pid));
  setInterval(() => {{}}, 1000);
`;
const child = spawn(process.execPath, [
  "-e",
  childScript,
  {str(child_stopped)!r},
  {str(ready)!r},
], {{
  stdio: "ignore",
}});
const shutdownSignal = process.platform === "win32" ? "SIGBREAK" : "SIGTERM";
process.once(shutdownSignal, () => {{
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
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 87:
                return False
            raise
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
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )
        else:
            os.kill(child_pid, signal.SIGKILL)
        pytest.fail("Runner child survived cancellation")
