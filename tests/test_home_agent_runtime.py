from __future__ import annotations

import asyncio

import pytest

from lunar_agent import home_agent as home_module
from lunar_agent.models import HomeAgentRespondRequest


def _request() -> HomeAgentRespondRequest:
    return HomeAgentRespondRequest(
        threadId="home-thread-1",
        turnId="turn-1",
        clientId="client-1",
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


def test_home_agent_request_accepts_backend_graph_ref_shape():
    request = _request()

    assert request.selectedEntities[0].graph_ref == "nodes_vertex_collection/acme"


def test_home_agent_request_rejects_oversized_message():
    with pytest.raises(ValueError):
        HomeAgentRespondRequest(
            threadId="home-thread-1",
            turnId="turn-1",
            clientId="client-1",
            currentUserMessage="word " * 901,
        )


def test_project_root_uses_deployed_application_root(tmp_path, monkeypatch):
    runtime = tmp_path / "codex_runtime"
    runtime.mkdir()
    (runtime / "runner.mjs").write_text("// deployed runner", encoding="utf-8")
    monkeypatch.setenv("LUNAR_AGENT_APP_ROOT", str(tmp_path))

    assert home_module._project_root() == tmp_path.resolve()
    assert home_module._runner_path() == runtime / "runner.mjs"


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
  entities: [{id: "acme", type: "company", label: "Acme"}],
  citations: []
}) + "\\n");
""".strip(),
        encoding="utf-8",
    )
    mcp = tmp_path / "fake-mcp.mjs"
    mcp.write_text("// exists for runtime validation", encoding="utf-8")
    monkeypatch.setattr(home_module, "_runner_path", lambda: runner)
    monkeypatch.setattr(home_module, "_mcp_server_path", lambda: mcp)
    monkeypatch.setattr(home_module.settings, "home_agent_workspace_root", str(tmp_path / "work"))
    monkeypatch.setattr(home_module.settings, "backend_shared_token", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("LUNAR_AGENT_BACKEND_SHARED_TOKEN", "must-not-leak")
    events = []

    async def bootstrap(_request):
        return {
            "token": "delegated-read-only-token",
            "toolsUrl": "https://backend.example.test/api/v1/home/tools",
        }

    async def sink(event_type, data):
        events.append((event_type, data))

    response = asyncio.run(
        home_module.run_home_agent_turn(
            _request(),
            graph_bootstrap=bootstrap,
            event_sink=sink,
        )
    )

    assert response.final_response == "Grounded result"
    assert response.codex_thread_id == "codex-1"
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
    monkeypatch.setattr(home_module, "_runner_path", lambda: runner)
    monkeypatch.setattr(home_module, "_mcp_server_path", lambda: mcp)

    async def bootstrap(_request):
        return {"token": "", "toolsUrl": ""}

    with pytest.raises(RuntimeError, match="tool session"):
        asyncio.run(home_module.run_home_agent_turn(_request(), graph_bootstrap=bootstrap))


def test_runtime_cancellation_terminates_node_process(tmp_path, monkeypatch):
    runner = tmp_path / "waiting-runner.mjs"
    stopped = tmp_path / "stopped.txt"
    runner.write_text(
        f"""
import fs from "node:fs";
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
    monkeypatch.setattr(home_module, "_runner_path", lambda: runner)
    monkeypatch.setattr(home_module, "_mcp_server_path", lambda: mcp)
    monkeypatch.setattr(home_module.settings, "home_agent_workspace_root", str(tmp_path / "work"))

    async def bootstrap(_request):
        return {
            "token": "delegated-read-only-token",
            "toolsUrl": "https://backend.example.test/api/v1/home/tools",
        }

    async def scenario():
        task = asyncio.create_task(
            home_module.run_home_agent_turn(_request(), graph_bootstrap=bootstrap)
        )
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert stopped.read_text(encoding="utf-8") == "stopped"
