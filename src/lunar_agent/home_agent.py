from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .config import settings
from .models import HomeAgentRespondRequest, HomeAgentRespondResponse


logger = logging.getLogger(__name__)
_MAX_RUNNER_LINE_BYTES = 1_000_000
_MAX_RUNNER_STDERR_CHARS = 12_000
_EVENT_TYPES = {
    "plan.updated",
    "tool.started",
    "tool.progress",
    "tool.completed",
    "entity.upserted",
    "citation.upserted",
    "assistant.delta",
}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _runner_path() -> Path:
    return _project_root() / "codex_runtime" / "runner.mjs"


def _mcp_server_path() -> Path:
    return _project_root() / "codex_runtime" / "lunar_graph_mcp.mjs"


def _node_binary() -> str:
    configured = str(settings.codex_node_binary or "node").strip()
    return shutil.which(configured) or configured


def _codex_home() -> str:
    configured = str(settings.codex_home or "").strip()
    return configured or "/codex-auth"


def _workspace_for_thread(thread_id: str) -> Path:
    digest = hashlib.sha256(str(thread_id).encode("utf-8")).hexdigest()[:24]
    root = Path(settings.home_agent_workspace_root).expanduser().resolve()
    workspace = root / digest
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = workspace / "README.md"
    if not marker.exists():
        marker.write_text(
            "# LunarChain Home investigation workspace\n\n"
            "This isolated workspace is available for temporary analysis files and commands. "
            "Do not place credentials or persistent customer exports here.\n",
            encoding="utf-8",
        )
    return workspace


def _safe_runner_env() -> dict[str, str]:
    path = os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    home = os.getenv("HOME", str(Path.home()))
    return {
        "PATH": path,
        "HOME": home,
        "CODEX_HOME": _codex_home(),
        "LANG": os.getenv("LANG", "C.UTF-8"),
        "LC_ALL": os.getenv("LC_ALL", "C.UTF-8"),
        "NO_COLOR": "1",
    }


async def _post_backend(
    path: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    base_url = str(settings.backend_base_url or "").strip()
    shared_token = str(settings.backend_shared_token or "").strip()
    if not base_url or not shared_token:
        raise RuntimeError("Home Agent backend bridge is not configured")
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=10.0)) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}{path}",
            headers={"Authorization": f"Bearer {shared_token}"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Home Agent backend returned an invalid payload")
    return data


async def _bootstrap_graph_tools(request: HomeAgentRespondRequest) -> dict[str, Any]:
    return await _post_backend(
        "/api/v1/home/internal/tool-sessions",
        {
            "threadId": request.threadId,
            "turnId": request.turnId,
            "clientId": request.clientId,
        },
        timeout_seconds=min(max(float(settings.backend_http_timeout), 10.0), 90.0),
    )


async def _forward_event(
    request: HomeAgentRespondRequest,
    event_type: str,
    data: dict[str, Any],
) -> None:
    if event_type not in _EVENT_TYPES:
        return
    await _post_backend(
        "/api/v1/home/internal/events",
        {
            "thread_id": request.threadId,
            "turn_id": request.turnId,
            "event_type": event_type,
            "data": data,
        },
        timeout_seconds=min(max(float(settings.backend_http_timeout), 10.0), 90.0),
    )


async def _read_stderr(stream: asyncio.StreamReader) -> str:
    chunks: list[str] = []
    size = 0
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        decoded = chunk.decode("utf-8", errors="replace")
        remaining = _MAX_RUNNER_STDERR_CHARS - size
        if remaining > 0:
            chunks.append(decoded[:remaining])
            size += len(decoded[:remaining])
    return "".join(chunks)


async def run_home_agent_turn(
    request: HomeAgentRespondRequest,
    *,
    event_sink: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    graph_bootstrap: Callable[[HomeAgentRespondRequest], Awaitable[dict[str, Any]]] | None = None,
) -> HomeAgentRespondResponse:
    if not settings.home_agent_enabled:
        raise RuntimeError("Home Agent runtime is disabled")
    runner_path = _runner_path()
    mcp_path = _mcp_server_path()
    if not runner_path.is_file() or not mcp_path.is_file():
        raise RuntimeError("Codex runtime files are missing")

    bootstrap = await (graph_bootstrap or _bootstrap_graph_tools)(request)
    delegated_token = str(bootstrap.get("token") or "").strip()
    graph_tools_url = str(bootstrap.get("toolsUrl") or "").strip()
    if not delegated_token or not graph_tools_url:
        raise RuntimeError("LunarGraph tool session could not be established")

    workspace = _workspace_for_thread(request.threadId)
    payload = {
        "threadId": request.threadId,
        "turnId": request.turnId,
        "codexThreadId": request.codexThreadId,
        "clientId": request.clientId,
        "currentUserMessage": request.currentUserMessage,
        "selectedEntities": [item.model_dump() for item in request.selectedEntities],
        "conversationHistory": [item.model_dump() for item in request.conversationHistory],
        "model": str(settings.home_agent_model or "gpt-5.6-sol").strip(),
        "reasoningEffort": str(settings.home_agent_reasoning_effort or "ultra").strip(),
        "workspace": str(workspace),
        "codexHome": _codex_home(),
        "codexPath": str(settings.codex_cli_path or "").strip() or None,
        "nodeBinary": _node_binary(),
        "mcpServerPath": str(mcp_path),
        "graphToolsUrl": graph_tools_url,
        "graphDelegatedToken": delegated_token,
    }

    process = await asyncio.create_subprocess_exec(
        _node_binary(),
        str(runner_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_safe_runner_env(),
        cwd=str(_project_root()),
        limit=_MAX_RUNNER_LINE_BYTES + 1,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        raise RuntimeError("Codex runtime streams could not be opened")

    process.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()
    stderr_task = asyncio.create_task(_read_stderr(process.stderr))
    result: dict[str, Any] | None = None
    sink = event_sink or (lambda event_type, data: _forward_event(request, event_type, data))

    try:
        async with asyncio.timeout(max(30, min(int(settings.home_agent_timeout), 1800))):
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                if len(line) > _MAX_RUNNER_LINE_BYTES:
                    raise RuntimeError("Codex runtime emitted an oversized event")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Ignoring invalid Codex runtime output")
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("kind") == "event":
                    event_type = str(message.get("eventType") or "").strip()
                    data = message.get("data")
                    if event_type in _EVENT_TYPES and isinstance(data, dict):
                        try:
                            await sink(event_type, data)
                        except Exception as exc:
                            logger.warning("Home Agent event forwarding failed: %s", type(exc).__name__)
                elif message.get("kind") == "result":
                    result = message
            return_code = await process.wait()
    except BaseException:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise
    finally:
        stderr = await stderr_task

    if process.returncode != 0 or result is None:
        if stderr:
            logger.error(
                "Codex Home runtime failed (exit=%s): %s",
                process.returncode,
                stderr[-2000:],
            )
        raise RuntimeError("Codex Home runtime failed")

    return HomeAgentRespondResponse(
        final_response=str(result.get("finalResponse") or "").strip()[:60000],
        codex_thread_id=str(result.get("codexThreadId") or "").strip() or None,
        model=str(result.get("model") or settings.home_agent_model).strip() or None,
        entities=list(result.get("entities") or [])[:100],
        citations=list(result.get("citations") or [])[:100],
    )


async def codex_auth_status() -> dict[str, Any]:
    codex_home = Path(_codex_home()).expanduser()
    auth_file_present = (codex_home / "auth.json").is_file()
    cli = str(settings.codex_cli_path or "").strip()
    if not cli:
        cli = str(_project_root() / "node_modules" / "@openai" / "codex" / "bin" / "codex.js")
    if not Path(cli).is_file() and not shutil.which(cli):
        return {"configured": False, "mode": "chatgpt", "detail": "Codex CLI is unavailable"}
    if not auth_file_present:
        return {
            "configured": False,
            "mode": "chatgpt",
            "detail": "ChatGPT-managed Codex authentication is not mounted",
        }
    return {"configured": True, "mode": "chatgpt", "detail": "ChatGPT-managed Codex authentication"}
