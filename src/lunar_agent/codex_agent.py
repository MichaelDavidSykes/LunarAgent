from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import signal
import shutil
import subprocess
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
    "runtime_policy_violation": (
        "The investigation attempted an unavailable local execution capability and was stopped."
    ),
    "runtime_timeout": (
        "The investigation exceeded its secure runtime limit. Narrow the request and retry."
    ),
    "runtime_unavailable": (
        "The Codex investigation runtime is temporarily unavailable. Retry this turn."
    ),
}
_codex_auth_failure_fingerprint: tuple[int, int, int] | None = None
_codex_auth_failure_retry_at = 0.0
_codex_auth_probe_cache: tuple[tuple[int, int, int], str | None, float] | None = None
_CODEX_AUTH_FAILURE_RECHECK_SECONDS = 60.0
_area_risk_codex_semaphore = asyncio.Semaphore(1)
_MAX_AREA_RISK_RUNNER_OUTPUT_BYTES = 256_000


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
    if "explorer runtime policy blocked local" in text:
        return "runtime_policy_violation"
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
    global _codex_auth_failure_fingerprint, _codex_auth_failure_retry_at
    global _codex_auth_probe_cache
    _codex_auth_failure_fingerprint = _codex_auth_file_fingerprint()
    _codex_auth_failure_retry_at = (
        time.monotonic() + _CODEX_AUTH_FAILURE_RECHECK_SECONDS
    )
    _codex_auth_probe_cache = None


def _clear_codex_auth_failure() -> None:
    global _codex_auth_failure_fingerprint, _codex_auth_failure_retry_at
    _codex_auth_failure_fingerprint = None
    _codex_auth_failure_retry_at = 0.0


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


def execution_policy_status() -> dict[str, Any]:
    policy_path = _project_root() / "codex_runtime" / "execution_policy.json"
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExplorerCodexRuntimeError("runtime_unavailable") from exc
    if payload != {
        "schema": 1,
        "mode": "read-only-no-host-exec",
        "sandboxMode": "read-only",
        "networkAccessEnabled": False,
        "hostCommands": False,
        "fileWrites": False,
    }:
        raise ExplorerCodexRuntimeError("runtime_unavailable")
    return {
        "configured": True,
        "mode": payload["mode"],
        "verified": True,
        "sandboxMode": payload["sandboxMode"],
        "networkAccessEnabled": payload["networkAccessEnabled"],
        "hostCommands": payload["hostCommands"],
        "fileWrites": payload["fileWrites"],
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


async def _terminate_process(
    process: asyncio.subprocess.Process,
    *,
    process_group: bool = False,
) -> None:
    if process.returncode is not None:
        return

    def send_signal(sig: signal.Signals) -> None:
        try:
            if process_group and os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            elif process_group:
                os.killpg(process.pid, sig)
            else:
                process.send_signal(sig)
        except ProcessLookupError:
            pass

    send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        if process_group and os.name == "nt":
            tree_killer = await asyncio.create_subprocess_exec(
                shutil.which("taskkill") or "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=_safe_runner_env(),
            )
            await tree_killer.wait()
        else:
            send_signal(signal.SIGKILL)
        if process.returncode is None:
            process.kill()
        await process.wait()
    else:
        # The process-group leader can exit before an MCP or Codex child that
        # ignored SIGTERM. A final group signal closes that race without
        # affecting the Agent because every runner is a dedicated session.
        if process_group and os.name != "nt":
            send_signal(signal.SIGKILL)


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


def _area_risk_runner_path() -> Path:
    return _project_root() / "codex_runtime" / "area_risk_runner.mjs"


def _mcp_server_path() -> Path:
    return _project_root() / "codex_runtime" / "lunar_graph_mcp.mjs"


def _node_binary() -> str:
    configured = str(settings.codex_node_binary or "node").strip()
    return shutil.which(configured) or configured


def _process_group_options() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


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
            "# LunarChain Explorer Agent private runtime directory\n\n"
            "This directory is read-only to the model. Local commands and "
            "user-directed file changes are unavailable.\n",
            encoding="utf-8",
        )
    return workspace


def _workspace_for_area_risk() -> Path:
    root = Path(settings.codex_agent_workspace_root).expanduser().resolve()
    workspace = root / "safe-route-area-risk"
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = workspace / "README.md"
    if not marker.exists():
        marker.write_text(
            "# SafeRoute area-risk evidence analysis\n\n"
            "This stateless workspace is read-only to the model. The prompt contains "
            "only bounded public evidence and sanitized area metadata.\n",
            encoding="utf-8",
        )
    return workspace


def _safe_runner_env() -> dict[str, str]:
    path = os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    home = os.getenv("HOME", str(Path.home()))
    environment = {
        "PATH": path,
        "HOME": home,
        "CODEX_HOME": _codex_home(),
        "LANG": os.getenv("LANG", "C.UTF-8"),
        "LC_ALL": os.getenv("LC_ALL", "C.UTF-8"),
        "NO_COLOR": "1",
    }
    # Node's Windows crypto initialization requires SystemRoot. Keep this
    # platform variable narrowly allowlisted without inheriting service secrets.
    if os.name == "nt":
        system_root = str(os.getenv("SystemRoot") or os.getenv("WINDIR") or "").strip()
        if system_root:
            environment["SystemRoot"] = system_root
    return environment


async def run_area_risk_codex_analysis(
    prompt: str,
    *,
    max_zones: int,
    evidence_urls: set[str] | None = None,
) -> dict[str, Any]:
    """Analyze bounded public area-risk evidence through ChatGPT-authenticated Codex."""
    if not settings.codex_agent_enabled or not settings.area_risk_account_enabled:
        raise ExplorerCodexRuntimeError("runtime_unavailable")
    execution_policy_status()
    runner_path = _area_risk_runner_path()
    if not runner_path.is_file():
        raise ExplorerCodexRuntimeError("runtime_unavailable")
    auth_fingerprint = _codex_auth_file_fingerprint()
    if auth_fingerprint is None:
        _mark_codex_auth_unavailable()
        raise ExplorerCodexRuntimeError("codex_auth_unavailable")
    if await _cached_codex_login_method(auth_fingerprint) != "chatgpt":
        _mark_codex_auth_unavailable()
        raise ExplorerCodexRuntimeError("codex_auth_unavailable")

    payload = {
        "prompt": str(prompt or "").strip()[:48000],
        "maxZones": max(1, min(int(max_zones or 1), 6)),
        "model": str(settings.area_risk_codex_model or settings.codex_agent_model or "gpt-5.6-sol").strip(),
        "reasoningEffort": str(settings.area_risk_codex_reasoning_effort or "low").strip(),
        "workspace": str(_workspace_for_area_risk()),
        "codexHome": _codex_home(),
        "codexPath": str(settings.codex_cli_path or "").strip() or None,
        "evidenceUrls": [
            str(item).strip()[:500]
            for item in sorted(evidence_urls or set())[:40]
            if str(item).strip()
        ],
    }
    if not payload["prompt"]:
        raise ValueError("Area-risk Codex prompt is required")

    process: asyncio.subprocess.Process | None = None
    try:
        async with _area_risk_codex_semaphore:
            process = await asyncio.create_subprocess_exec(
                _node_binary(),
                str(runner_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_safe_runner_env(),
                cwd=str(_project_root()),
                **_process_group_options(),
            )
            timeout_seconds = max(
                30,
                min(int(settings.area_risk_codex_timeout or 180), 600),
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                timeout=timeout_seconds,
            )
    except asyncio.TimeoutError as exc:
        if process is not None:
            await _terminate_process(process, process_group=True)
        raise ExplorerCodexRuntimeError("runtime_timeout") from exc
    except asyncio.CancelledError:
        if process is not None:
            await _terminate_process(process, process_group=True)
        raise
    except ExplorerCodexRuntimeError:
        raise
    except Exception as exc:
        code = _runtime_failure_code(exc)
        raise ExplorerCodexRuntimeError(code) from exc

    if (
        process is None
        or process.returncode != 0
        or len(stdout) > _MAX_AREA_RISK_RUNNER_OUTPUT_BYTES
        or len(stderr) > _MAX_RUNNER_STDERR_CHARS
    ):
        safe_stderr = stderr[:_MAX_RUNNER_STDERR_CHARS].decode("utf-8", errors="replace")
        code = _runtime_failure_code(
            RuntimeError("Area-risk Codex runtime exited without a valid result"),
            safe_stderr,
        )
        if code == "codex_auth_unavailable":
            _mark_codex_auth_unavailable()
        raise ExplorerCodexRuntimeError(code)
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExplorerCodexRuntimeError("runtime_unavailable") from exc
    if not isinstance(result, dict) or not isinstance(result.get("zones"), list):
        raise ExplorerCodexRuntimeError("runtime_unavailable")
    _clear_codex_auth_failure()
    return {
        "zones": result["zones"][: payload["maxZones"]],
        "notes": str(result.get("notes") or "").strip()[:1000],
        "model": str(result.get("model") or payload["model"]).strip()[:120],
        "webSearchCompleted": bool(result.get("webSearchCompleted")),
        "verifiedSourceUrls": [
            str(item).strip()[:500]
            for item in (result.get("verifiedSourceUrls") or [])[:24]
            if str(item).strip()
        ],
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


async def _forward_runtime_checkpoint(
    request: ExplorerAgentRespondRequest,
    codex_thread_id: str,
) -> bool:
    response = await _post_backend(
        "/api/v1/graph/ai-agent/tools/runtime-checkpoint",
        {
            "session_id": request.sessionId,
            "request_id": request.requestId,
            "codex_thread_id": codex_thread_id,
        },
        timeout_seconds=min(max(float(settings.backend_http_timeout), 10.0), 90.0),
    )
    return response.get("accepted") is True


def _runtime_result_fingerprint(response: ExplorerAgentRespondResponse) -> str:
    canonical = json.dumps(
        response.model_dump(mode="json", exclude_none=False),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _checkpointed_runtime_response(
    bootstrap: dict[str, Any],
    request: ExplorerAgentRespondRequest,
    codex_thread_id: str | None,
) -> ExplorerAgentRespondResponse | None:
    checkpoint = bootstrap.get("resultCheckpoint")
    if checkpoint is None:
        return None
    if not isinstance(checkpoint, dict):
        raise ExplorerCodexRuntimeError("graph_session_stale")
    if str(checkpoint.get("requestId") or "").strip() != str(
        request.requestId or ""
    ).strip():
        raise ExplorerCodexRuntimeError("graph_session_stale")
    checkpoint_thread_id = str(checkpoint.get("codexThreadId") or "").strip()
    if (
        not checkpoint_thread_id
        or not codex_thread_id
        or checkpoint_thread_id != codex_thread_id
    ):
        raise ExplorerCodexRuntimeError("graph_session_stale")
    response_payload = checkpoint.get("response")
    if not isinstance(response_payload, dict):
        raise ExplorerCodexRuntimeError("graph_session_stale")
    try:
        response = ExplorerAgentRespondResponse.model_validate(response_payload)
        fingerprint = _runtime_result_fingerprint(response)
    except Exception as exc:
        raise ExplorerCodexRuntimeError("graph_session_stale") from exc
    expected_fingerprint = str(
        checkpoint.get("resultFingerprint") or ""
    ).strip()
    if (
        len(expected_fingerprint) != 64
        or not secrets.compare_digest(fingerprint, expected_fingerprint)
        or str(response.codexThreadId or "").strip() != checkpoint_thread_id
    ):
        raise ExplorerCodexRuntimeError("graph_session_stale")
    return response


async def _forward_runtime_result_checkpoint(
    request: ExplorerAgentRespondRequest,
    response: ExplorerAgentRespondResponse,
) -> bool:
    payload = {
        "session_id": request.sessionId,
        "request_id": request.requestId,
        "codex_thread_id": response.codexThreadId,
        "result": response.model_dump(mode="json", exclude_none=False),
    }
    delays = (0.0, 0.25, 1.0)
    last_error: Exception | None = None
    for attempt, delay in enumerate(delays):
        if delay:
            await asyncio.sleep(delay)
        try:
            checkpoint_response = await _post_backend(
                "/api/v1/graph/ai-agent/tools/runtime-result-checkpoint",
                payload,
                timeout_seconds=min(
                    max(float(settings.backend_http_timeout), 10.0),
                    90.0,
                ),
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            last_error = exc
        else:
            return checkpoint_response.get("accepted") is True
        if attempt + 1 == len(delays):
            break
    if last_error is not None:
        raise last_error
    return False


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
    checkpoint_sink: Callable[[str], Awaitable[bool]] | None = None,
    result_checkpoint_sink: Callable[
        [ExplorerAgentRespondResponse],
        Awaitable[bool],
    ]
    | None = None,
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
    try:
        execution_policy_status()
    except ExplorerCodexRuntimeError as exc:
        await _emit_runtime_failure(sink, code=exc.code, duration_ms=0)
        raise
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

    requested_codex_thread_id = str(request.codexThreadId or "").strip()
    checkpointed_codex_thread_id = str(
        bootstrap.get("codexThreadId") or ""
    ).strip()
    if (
        requested_codex_thread_id
        and checkpointed_codex_thread_id
        and requested_codex_thread_id != checkpointed_codex_thread_id
    ):
        code = "graph_session_stale"
        duration_ms = int((time.monotonic() - started_at) * 1000)
        await _emit_runtime_failure(sink, code=code, duration_ms=duration_ms)
        raise ExplorerCodexRuntimeError(code)
    effective_codex_thread_id = (
        requested_codex_thread_id
        or checkpointed_codex_thread_id
        or None
    )
    try:
        checkpointed_response = _checkpointed_runtime_response(
            bootstrap,
            request,
            effective_codex_thread_id,
        )
    except ExplorerCodexRuntimeError as exc:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        await _emit_runtime_failure(
            sink,
            code=exc.code,
            duration_ms=duration_ms,
        )
        raise
    if checkpointed_response is not None:
        duration_ms = int((time.monotonic() - started_at) * 1000)
        telemetry_logger.info(
            (
                "Explorer Codex durable result replayed session=%s request=%s "
                "model=%s duration_ms=%s"
            ),
            session_fingerprint,
            request_fingerprint,
            checkpointed_response.model or settings.codex_agent_model,
            duration_ms,
        )
        _clear_codex_auth_failure()
        return checkpointed_response
    checkpoint = checkpoint_sink or (
        lambda codex_thread_id: _forward_runtime_checkpoint(
            request,
            codex_thread_id,
        )
    )
    workspace = _workspace_for_thread(request.sessionId)
    payload = {
        "threadId": request.sessionId,
        "turnId": request.requestId,
        "codexThreadId": effective_codex_thread_id,
        "clientId": request.clientId or request.quotaKey or request.sessionId,
        "currentUserMessage": request.currentUserMessage,
        "selectedEntities": [item.model_dump() for item in request.selectedEntities],
        "conversationHistory": [item.model_dump() for item in request.conversationHistory],
        "queryPreview": request.queryPreview,
        "querySummary": request.querySummary,
        "queryContext": request.queryContext,
        "investigationKnowledge": request.investigationKnowledge,
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
            **_process_group_options(),
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
        await _terminate_process(process, process_group=True)
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
                if (
                    message.get("kind") == "checkpoint"
                    and message.get("checkpointType") == "codex_thread"
                ):
                    codex_thread_id = str(
                        message.get("codexThreadId") or ""
                    ).strip()
                    if not codex_thread_id or len(codex_thread_id) > 180:
                        raise RuntimeError(
                            "Codex runtime emitted an invalid thread checkpoint"
                        )
                    try:
                        checkpoint_accepted = await checkpoint(codex_thread_id)
                    except Exception as exc:
                        # A transient checkpoint transport failure must not
                        # discard an otherwise healthy, read-only Codex turn.
                        # The final response still carries the thread id and
                        # the backend persists it on ordinary completion.
                        logger.warning(
                            "LunarAgent runtime checkpoint forwarding failed: %s",
                            type(exc).__name__,
                        )
                    else:
                        if not checkpoint_accepted:
                            raise RuntimeError(
                                "Explorer turn rejected its Codex runtime checkpoint"
                            )
                elif message.get("kind") == "event":
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
        await _terminate_process(process, process_group=True)
        telemetry_logger.info(
            "Explorer Codex turn cancelled session=%s request=%s duration_ms=%s",
            session_fingerprint,
            request_fingerprint,
            int((time.monotonic() - started_at) * 1000),
        )
        raise
    except Exception as exc:
        failure = exc
        await _terminate_process(process, process_group=True)
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

    response = ExplorerAgentRespondResponse(
        reply=str(result.get("finalResponse") or "").strip()[:60000],
        codexThreadId=str(result.get("codexThreadId") or "").strip() or None,
        model=str(result.get("model") or settings.codex_agent_model).strip() or None,
        actions=list(result.get("actions") or [])[:4],
        followUps=list(result.get("followUps") or [])[:4],
        entities=list(result.get("entities") or [])[:100],
        citations=list(result.get("citations") or [])[:100],
        media=list(result.get("media") or [])[:12],
        turnKnowledge=result.get("turnKnowledge") or {},
    )
    if not response.codexThreadId:
        raise ExplorerCodexRuntimeError("graph_session_stale")
    result_checkpoint = result_checkpoint_sink or (
        lambda final_response: _forward_runtime_result_checkpoint(
            request,
            final_response,
        )
    )
    try:
        result_checkpoint_accepted = await result_checkpoint(response)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Preserve a successfully completed read-only investigation if the
        # private checkpoint transport is briefly unavailable. The ordinary
        # response can still complete, and an in-process exact retry joins the
        # retained canonical result. No consequential action is executed here.
        logger.warning(
            "LunarAgent final-result checkpoint forwarding failed: %s",
            type(exc).__name__,
        )
    else:
        if not result_checkpoint_accepted:
            raise ExplorerCodexRuntimeError("graph_session_stale")

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

    return response


async def codex_auth_status() -> dict[str, Any]:
    global _codex_auth_failure_retry_at
    auth_fingerprint = _codex_auth_file_fingerprint()
    if not _codex_cli_command():
        return {"configured": False, "mode": "chatgpt", "detail": "Codex CLI is unavailable"}
    if auth_fingerprint is None:
        return {
            "configured": False,
            "mode": "chatgpt",
            "detail": "ChatGPT-managed Codex authentication is not mounted",
        }
    auth_failure_is_current = (
        _codex_auth_failure_fingerprint == auth_fingerprint
    )
    if (
        auth_failure_is_current
        and time.monotonic() < _codex_auth_failure_retry_at
    ):
        return {
            "configured": False,
            "mode": "chatgpt",
            "detail": "ChatGPT-managed Codex authentication must be reconnected",
        }
    if await _cached_codex_login_method(auth_fingerprint) != "chatgpt":
        if auth_failure_is_current:
            _codex_auth_failure_retry_at = (
                time.monotonic() + _CODEX_AUTH_FAILURE_RECHECK_SECONDS
            )
        return {
            "configured": False,
            "mode": "chatgpt",
            "detail": (
                "ChatGPT-managed Codex authentication must be reconnected"
                if auth_failure_is_current
                else "ChatGPT-managed Codex authentication is unavailable"
            ),
        }
    _clear_codex_auth_failure()
    return {"configured": True, "mode": "chatgpt", "detail": "ChatGPT-managed Codex authentication"}
