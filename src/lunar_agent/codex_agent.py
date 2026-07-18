from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .config import settings
from .models import ExplorerAgentRespondRequest, ExplorerAgentRespondResponse


logger = logging.getLogger(__name__)
telemetry_logger = logging.getLogger("uvicorn.error")
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
_RETRYABLE_FAILURE_CODES = {
    "codex_auth_unavailable",
    "codex_usage_limited",
    "graph_bridge_unavailable",
    "graph_tool_rate_limited",
    "command_sandbox_unavailable",
    "runtime_timeout",
    "runtime_unavailable",
}
_SAFE_FAILURE_MESSAGES = {
    "codex_auth_unavailable": (
        "ChatGPT-managed Codex authentication is unavailable. The service operator must reconnect it."
    ),
    "codex_usage_limited": (
        "The Codex runtime is temporarily usage limited. Retry after the plan limit resets."
    ),
    "delegated_token_rejected": (
        "The delegated LunarGraph authorization was rejected. Start a fresh Explorer turn."
    ),
    "graph_session_stale": (
        "This Explorer graph session is no longer active. Start a fresh turn."
    ),
    "graph_bridge_unavailable": (
        "The LunarGraph tool bridge is temporarily unavailable. Retry this investigation."
    ),
    "graph_tool_rate_limited": (
        "The LunarGraph tool bridge is temporarily rate limited. Retry this investigation shortly."
    ),
    "command_sandbox_unavailable": (
        "The isolated workspace command service is temporarily unavailable. Retry this investigation."
    ),
    "runtime_timeout": (
        "The investigation exceeded its secure runtime limit. Narrow the request and retry."
    ),
    "runtime_unavailable": (
        "The Codex investigation runtime is temporarily unavailable. Retry this turn."
    ),
}
_codex_auth_failure_fingerprint: tuple[int, int, int] | None = None
_codex_auth_probe_cache: tuple[tuple[int, int, int], str | None, float] | None = None


class ExplorerCodexRuntimeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if code in _SAFE_FAILURE_MESSAGES else "runtime_unavailable"
        super().__init__(_SAFE_FAILURE_MESSAGES[self.code])


def _request_fingerprint(value: str | None) -> str:
    return hashlib.sha256(str(value or "missing").encode("utf-8")).hexdigest()[:12]


def _runtime_failure_code(exc: BaseException, stderr: str = "") -> str:
    if isinstance(exc, TimeoutError):
        return "runtime_timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            return "delegated_token_rejected"
        if status == 409:
            return "graph_session_stale"
        if status == 429:
            return "graph_tool_rate_limited"
        if status >= 500:
            return "graph_bridge_unavailable"

    text = f"{type(exc).__name__} {exc} {stderr}".casefold()
    if any(
        marker in text
        for marker in (
            "login required",
            "not logged in",
            "unauthorized",
            "authentication expired",
            "refresh token",
            "chatgpt authentication",
            "codex authentication",
        )
    ):
        return "codex_auth_unavailable"
    if any(
        marker in text
        for marker in (
            "usage limit",
            "rate limit",
            "rate_limit",
            "too many requests",
            "insufficient_quota",
        )
    ):
        return "codex_usage_limited"
    if any(marker in text for marker in ("timed out", "timeout", "timeouterror")):
        return "runtime_timeout"
    if any(marker in text for marker in ("command broker", "command sandbox")):
        return "command_sandbox_unavailable"
    if any(
        marker in text
        for marker in (
            "mcp",
            "lunargraph",
            "graph tool",
            "backend bridge",
            "connection refused",
            "connecterror",
        )
    ):
        return "graph_bridge_unavailable"
    return "runtime_unavailable"


def _codex_auth_file() -> Path:
    return Path(_codex_home()).expanduser() / "auth.json"


def _codex_auth_file_fingerprint() -> tuple[int, int, int] | None:
    try:
        stat = _codex_auth_file().stat()
    except OSError:
        return None
    return (int(stat.st_ino), int(stat.st_mtime_ns), int(stat.st_size))


def _mark_codex_auth_unavailable() -> None:
    global _codex_auth_failure_fingerprint, _codex_auth_probe_cache
    _codex_auth_failure_fingerprint = _codex_auth_file_fingerprint()
    _codex_auth_probe_cache = None


def _clear_codex_auth_failure() -> None:
    global _codex_auth_failure_fingerprint
    _codex_auth_failure_fingerprint = None


def _codex_cli_command() -> list[str] | None:
    configured = str(settings.codex_cli_path or "").strip()
    candidate = Path(configured).expanduser() if configured else (
        _project_root() / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    )
    if candidate.is_file():
        return [_node_binary(), str(candidate)] if candidate.suffix == ".js" else [str(candidate)]
    resolved = shutil.which(configured) if configured else shutil.which("codex")
    return [resolved] if resolved else None


async def _codex_login_method() -> str | None:
    command = _codex_cli_command()
    if not command:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            "login",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_safe_runner_env(),
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3)
    except asyncio.TimeoutError:
        if "process" in locals():
            await _terminate_process(process)
        return None
    except Exception:
        return None
    if process.returncode != 0:
        return None
    status_text = b"\n".join((stdout[:1000], stderr[:1000])).decode(
        "utf-8",
        errors="replace",
    ).casefold()
    if "logged in using chatgpt" in status_text:
        return "chatgpt"
    if "logged in using" in status_text and "api key" in status_text:
        return "api"
    return None


async def _cached_codex_login_method(
    auth_fingerprint: tuple[int, int, int],
) -> str | None:
    global _codex_auth_probe_cache
    now = time.monotonic()
    if (
        _codex_auth_probe_cache
        and _codex_auth_probe_cache[0] == auth_fingerprint
        and _codex_auth_probe_cache[2] > now
    ):
        return _codex_auth_probe_cache[1]
    method = await _codex_login_method()
    _codex_auth_probe_cache = (auth_fingerprint, method, now + 10)
    return method


async def command_broker_status() -> dict[str, Any]:
    socket_path = str(settings.command_broker_socket or "").strip()
    token = str(settings.command_broker_token or "").strip()
    if not socket_path or not token or not Path(socket_path).is_socket():
        raise ExplorerCodexRuntimeError("command_sandbox_unavailable")
    try:
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        async with httpx.AsyncClient(transport=transport, timeout=3.0) as client:
            response = await client.get(
                "http://command-broker/live",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise ExplorerCodexRuntimeError("command_sandbox_unavailable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ok"
        or payload.get("verification") != "executable"
    ):
        raise ExplorerCodexRuntimeError("command_sandbox_unavailable")
    return {
        "configured": True,
        "mode": "isolated-workspace",
        "verified": True,
    }


async def _emit_runtime_failure(
    sink: Callable[[str, dict[str, Any]], Awaitable[None]],
    *,
    code: str,
    duration_ms: int,
) -> None:
    if code == "codex_auth_unavailable":
        _mark_codex_auth_unavailable()
    try:
        await sink(
            "tool.progress",
            {
                "phase": "runtime",
                "status": "failed",
                "errorCode": code,
                "message": _SAFE_FAILURE_MESSAGES.get(
                    code,
                    _SAFE_FAILURE_MESSAGES["runtime_unavailable"],
                ),
                "durationMs": max(0, duration_ms),
                "retryable": code in _RETRYABLE_FAILURE_CODES,
            },
        )
    except Exception:
        logger.debug("LunarAgent could not forward the safe runtime failure event", exc_info=True)


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


def _project_root() -> Path:
    configured_root = str(os.getenv("LUNAR_AGENT_APP_ROOT") or "").strip()
    candidates = [
        Path(configured_root).expanduser() if configured_root else None,
        Path.cwd(),
        Path(__file__).resolve().parents[2],
    ]
    for candidate in candidates:
        if candidate and (candidate / "codex_runtime" / "runner.mjs").is_file():
            return candidate.resolve()
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
    root = Path(settings.codex_agent_workspace_root).expanduser().resolve()
    workspace = root / digest
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = workspace / "README.md"
    if not marker.exists():
        marker.write_text(
            "# LunarChain Explorer Agent investigation workspace\n\n"
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
        raise RuntimeError("LunarAgent backend bridge is not configured")
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=10.0)) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}{path}",
            headers={"Authorization": f"Bearer {shared_token}"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("LunarAgent backend returned an invalid payload")
    return data


async def _bootstrap_graph_tools(request: ExplorerAgentRespondRequest) -> dict[str, Any]:
    return await _post_backend(
        "/api/v1/graph/ai-agent/tools/codex-session",
        {
            "session_id": request.sessionId,
            "request_id": request.requestId,
            "client_id": request.clientId,
        },
        timeout_seconds=min(max(float(settings.backend_http_timeout), 10.0), 90.0),
    )


async def _forward_event(
    request: ExplorerAgentRespondRequest,
    event_type: str,
    data: dict[str, Any],
) -> None:
    if event_type not in _EVENT_TYPES:
        return
    await _post_backend(
        "/api/v1/graph/ai-agent/tools/activity",
        {
            "session_id": request.sessionId,
            "request_id": request.requestId,
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


async def run_explorer_codex_turn(
    request: ExplorerAgentRespondRequest,
    *,
    event_sink: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    graph_bootstrap: Callable[[ExplorerAgentRespondRequest], Awaitable[dict[str, Any]]] | None = None,
) -> ExplorerAgentRespondResponse:
    started_at = time.monotonic()
    session_fingerprint = _request_fingerprint(request.sessionId)
    request_fingerprint = _request_fingerprint(request.requestId)
    sink = event_sink or (lambda event_type, data: _forward_event(request, event_type, data))
    telemetry_logger.info(
        "Explorer Codex turn started session=%s request=%s model=%s reasoning=%s",
        session_fingerprint,
        request_fingerprint,
        settings.codex_agent_model,
        settings.codex_agent_reasoning_effort,
    )
    if not settings.codex_agent_enabled:
        raise RuntimeError("LunarAgent Codex runtime is disabled")
    if not str(request.sessionId or "").strip() or not str(request.requestId or "").strip():
        raise RuntimeError("Explorer session and request identifiers are required")
    command_broker_socket = str(settings.command_broker_socket or "").strip()
    command_broker_token = str(settings.command_broker_token or "").strip()
    if not command_broker_socket or not command_broker_token:
        code = "command_sandbox_unavailable"
        await _emit_runtime_failure(sink, code=code, duration_ms=0)
        raise ExplorerCodexRuntimeError(code)
    runner_path = _runner_path()
    mcp_path = _mcp_server_path()
    if not runner_path.is_file() or not mcp_path.is_file():
        raise RuntimeError("Codex runtime files are missing")

    try:
        bootstrap = await (graph_bootstrap or _bootstrap_graph_tools)(request)
    except asyncio.CancelledError:
        telemetry_logger.info(
            "Explorer Codex turn cancelled before graph bootstrap session=%s request=%s duration_ms=%s",
            session_fingerprint,
            request_fingerprint,
            int((time.monotonic() - started_at) * 1000),
        )
        raise
    except Exception as exc:
        code = _runtime_failure_code(exc)
        duration_ms = int((time.monotonic() - started_at) * 1000)
        await _emit_runtime_failure(sink, code=code, duration_ms=duration_ms)
        telemetry_logger.warning(
            "Explorer Codex graph bootstrap failed session=%s request=%s code=%s duration_ms=%s",
            session_fingerprint,
            request_fingerprint,
            code,
            duration_ms,
        )
        raise ExplorerCodexRuntimeError(code) from exc
    delegated_token = str(bootstrap.get("token") or "").strip()
    graph_tools_url = str(bootstrap.get("toolsUrl") or "").strip()
    if not delegated_token or not graph_tools_url:
        code = "graph_bridge_unavailable"
        duration_ms = int((time.monotonic() - started_at) * 1000)
        await _emit_runtime_failure(sink, code=code, duration_ms=duration_ms)
        telemetry_logger.warning(
            "Explorer Codex graph bootstrap incomplete session=%s request=%s duration_ms=%s",
            session_fingerprint,
            request_fingerprint,
            duration_ms,
        )
        raise ExplorerCodexRuntimeError(code)

    workspace = _workspace_for_thread(request.sessionId)
    payload = {
        "threadId": request.sessionId,
        "turnId": request.requestId,
        "codexThreadId": request.codexThreadId,
        "clientId": request.clientId or request.quotaKey or request.sessionId,
        "currentUserMessage": request.currentUserMessage,
        "selectedEntities": [item.model_dump() for item in request.selectedEntities],
        "conversationHistory": [item.model_dump() for item in request.conversationHistory],
        "queryPreview": request.queryPreview,
        "querySummary": request.querySummary,
        "queryContext": request.queryContext,
        "allowUiActions": bool(request.allowUiActions),
        "model": str(settings.codex_agent_model or "gpt-5.6-sol").strip(),
        "reasoningEffort": str(settings.codex_agent_reasoning_effort or "medium").strip(),
        "workspace": str(workspace),
        "codexHome": _codex_home(),
        "codexPath": str(settings.codex_cli_path or "").strip() or None,
        "nodeBinary": _node_binary(),
        "mcpServerPath": str(mcp_path),
        "graphToolsUrl": graph_tools_url,
        "graphDelegatedToken": delegated_token,
        "commandBrokerSocket": command_broker_socket,
        "commandBrokerToken": command_broker_token,
        "commandWorkspaceId": workspace.name,
    }

    try:
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
    except Exception as exc:
        code = _runtime_failure_code(exc)
        duration_ms = int((time.monotonic() - started_at) * 1000)
        await _emit_runtime_failure(sink, code=code, duration_ms=duration_ms)
        telemetry_logger.error(
            "Explorer Codex process launch failed session=%s request=%s code=%s duration_ms=%s",
            session_fingerprint,
            request_fingerprint,
            code,
            duration_ms,
        )
        raise ExplorerCodexRuntimeError(code) from exc
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        raise RuntimeError("Codex runtime streams could not be opened")

    process.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()
    stderr_task = asyncio.create_task(_read_stderr(process.stderr))
    result: dict[str, Any] | None = None
    failure: Exception | None = None

    try:
        async with asyncio.timeout(max(30, min(int(settings.codex_agent_timeout), 1800))):
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
                            logger.warning("LunarAgent event forwarding failed: %s", type(exc).__name__)
                elif message.get("kind") == "result":
                    result = message
            await process.wait()
    except asyncio.CancelledError:
        await _terminate_process(process)
        telemetry_logger.info(
            "Explorer Codex turn cancelled session=%s request=%s duration_ms=%s",
            session_fingerprint,
            request_fingerprint,
            int((time.monotonic() - started_at) * 1000),
        )
        raise
    except Exception as exc:
        failure = exc
        await _terminate_process(process)
    finally:
        stderr = await stderr_task

    if failure is not None or process.returncode != 0 or result is None:
        runtime_exc = failure or RuntimeError("Codex runtime exited without a result")
        code = _runtime_failure_code(runtime_exc, stderr)
        duration_ms = int((time.monotonic() - started_at) * 1000)
        await _emit_runtime_failure(sink, code=code, duration_ms=duration_ms)
        telemetry_logger.error(
            (
                "Explorer Codex turn failed session=%s request=%s code=%s "
                "duration_ms=%s exit=%s stderr_chars=%s stderr_sha256=%s"
            ),
            session_fingerprint,
            request_fingerprint,
            code,
            duration_ms,
            process.returncode,
            len(stderr),
            hashlib.sha256(stderr.encode("utf-8")).hexdigest()[:12] if stderr else "none",
        )
        raise ExplorerCodexRuntimeError(code) from runtime_exc

    duration_ms = int((time.monotonic() - started_at) * 1000)
    telemetry_logger.info(
        (
            "Explorer Codex turn completed session=%s request=%s model=%s duration_ms=%s "
            "entities=%s citations=%s actions=%s"
        ),
        session_fingerprint,
        request_fingerprint,
        str(result.get("model") or settings.codex_agent_model).strip(),
        duration_ms,
        len(result.get("entities") or []),
        len(result.get("citations") or []),
        len(result.get("actions") or []),
    )
    _clear_codex_auth_failure()

    return ExplorerAgentRespondResponse(
        reply=str(result.get("finalResponse") or "").strip()[:60000],
        codexThreadId=str(result.get("codexThreadId") or "").strip() or None,
        model=str(result.get("model") or settings.codex_agent_model).strip() or None,
        actions=list(result.get("actions") or [])[:4],
        followUps=list(result.get("followUps") or [])[:4],
        entities=list(result.get("entities") or [])[:100],
        citations=list(result.get("citations") or [])[:100],
    )


async def codex_auth_status() -> dict[str, Any]:
    auth_fingerprint = _codex_auth_file_fingerprint()
    if not _codex_cli_command():
        return {"configured": False, "mode": "chatgpt", "detail": "Codex CLI is unavailable"}
    if auth_fingerprint is None:
        return {
            "configured": False,
            "mode": "chatgpt",
            "detail": "ChatGPT-managed Codex authentication is not mounted",
        }
    if _codex_auth_failure_fingerprint == auth_fingerprint:
        return {
            "configured": False,
            "mode": "chatgpt",
            "detail": "ChatGPT-managed Codex authentication must be reconnected",
        }
    if await _cached_codex_login_method(auth_fingerprint) != "chatgpt":
        return {
            "configured": False,
            "mode": "chatgpt",
            "detail": "ChatGPT-managed Codex authentication is unavailable",
        }
    _clear_codex_auth_failure()
    return {"configured": True, "mode": "chatgpt", "detail": "ChatGPT-managed Codex authentication"}
