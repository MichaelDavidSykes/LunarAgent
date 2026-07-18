from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import shlex
import signal
import time
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)
app = FastAPI(title="LunarAgent command broker", docs_url=None, redoc_url=None)

_WORKSPACE_ID = re.compile(r"^[a-f0-9]{24}$")
_MAX_CAPTURE_BYTES = 16_000
_MAX_STREAM_BYTES = 128_000
_COMMAND_CONCURRENCY = asyncio.Semaphore(2)
_HEALTH_PROBE_LOCK = asyncio.Lock()
_HEALTH_PROBE_TTL_SECONDS = 30.0
_HEALTH_WORKSPACE_ID = "0" * 24
_HEALTH_MARKER = "LUNAR_AGENT_COMMAND_SANDBOX_READY"
_last_health_success_monotonic = 0.0
# The host broker and authentication-bearing container share UID 10001 for the
# protected broker socket (the host workspace itself is not container-mounted).
# RLIMIT_NPROC is charged across that UID, including Codex/Node threads. Two
# active Codex turns can therefore exceed a traditional per-command limit of 64
# before Bubblewrap starts. The broker's systemd TasksMax=160 remains the
# tighter command-service cgroup boundary; this UID-wide limit prevents false
# namespace failures.
_COMMAND_NPROC_LIMIT = 256
_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]*"
        ),
        r"\1[redacted]",
    ),
    (
        re.compile(
            r"(?i)(\b(?:password|passwd|secret|client[_-]?secret|api[_-]?key|"
            r"access[_-]?token|refresh[_-]?token|session[_-]?token|delegated[_-]?token|"
            r"bearer[_-]?token|private[_-]?key)\b\s*[:=]\s*)"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
        ),
        r"\1[redacted]",
    ),
    (
        re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
        r"\1 [redacted]",
    ),
    (
        re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{12,}\b"),
        "[redacted]",
    ),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "[redacted]",
    ),
)


class CommandRequest(BaseModel):
    workspaceId: str = Field(..., min_length=24, max_length=24)
    command: str = Field(..., min_length=1, max_length=4_000)
    timeoutSeconds: int = Field(default=20, ge=1, le=45)


class CommandResponse(BaseModel):
    status: Literal["completed", "failed", "timed_out", "output_limited"]
    exitCode: int | None = None
    stdout: str = ""
    stderr: str = ""
    durationMs: int
    outputTruncated: bool = False


class _OutputLimitExceeded(RuntimeError):
    pass


def _broker_token() -> str:
    return str(os.getenv("LUNAR_AGENT_COMMAND_BROKER_TOKEN") or "").strip()


def _workspace_root() -> Path:
    configured = str(
        os.getenv("LUNAR_AGENT_COMMAND_WORKSPACE_ROOT")
        or "/var/lib/lunar-agent/workspaces"
    ).strip()
    return Path(configured).expanduser().resolve()


def _bubblewrap_binary() -> Path:
    configured = str(
        os.getenv("LUNAR_AGENT_BWRAP_BINARY")
        or "/opt/lunar-agent-command-broker/bin/bwrap"
    ).strip()
    return Path(configured)


def _prlimit_binary() -> Path:
    configured = str(os.getenv("LUNAR_AGENT_PRLIMIT_BINARY") or "/usr/bin/prlimit").strip()
    return Path(configured)


def _redact_sensitive_text(value: str) -> str:
    text = str(value or "")
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _require_token(authorization: str | None = Header(default=None)) -> None:
    expected = _broker_token()
    if not expected:
        raise HTTPException(status_code=503, detail="Command broker authentication is unavailable")
    scheme, separator, credential = str(authorization or "").strip().partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not credential
        or not secrets.compare_digest(credential.encode(), expected.encode())
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _prepare_workspace(
    workspace_id: str,
    *,
    allow_health_workspace: bool = False,
) -> Path:
    if not _WORKSPACE_ID.fullmatch(str(workspace_id or "")):
        raise HTTPException(status_code=400, detail="Invalid command workspace")
    if workspace_id == _HEALTH_WORKSPACE_ID and not allow_health_workspace:
        raise HTTPException(status_code=400, detail="Invalid command workspace")
    root = _workspace_root()
    if not root.is_dir():
        raise HTTPException(status_code=503, detail="Command workspace is unavailable")
    candidate = root / workspace_id
    try:
        candidate.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail="Command workspace is unavailable",
        ) from exc
    if candidate.is_symlink() or not candidate.is_dir():
        raise HTTPException(status_code=400, detail="Invalid command workspace")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail="Command workspace is unavailable",
        ) from exc
    if resolved.parent != root:
        raise HTTPException(status_code=400, detail="Invalid command workspace")
    try:
        resolved.chmod(0o700)
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail="Command workspace is unavailable",
        ) from exc
    return resolved


def _sandbox_argv(workspace: Path, command: str, timeout_seconds: int) -> list[str]:
    cpu_limit = max(2, min(int(timeout_seconds) + 2, 50))
    return [
        str(_prlimit_binary()),
        f"--cpu={cpu_limit}",
        "--as=536870912",
        f"--nproc={_COMMAND_NPROC_LIMIT}",
        "--nofile=64",
        "--fsize=20971520",
        "--core=0",
        "--",
        str(_bubblewrap_binary()),
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--unshare-net",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--bind",
        str(workspace),
        "/workspace",
        "--chdir",
        "/workspace",
        "--clearenv",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "LC_ALL",
        "C.UTF-8",
        "--hostname",
        "lunar-agent-command",
        "/usr/bin/bash",
        "--noprofile",
        "--norc",
        "-c",
        command,
    ]


async def _read_bounded(stream: asyncio.StreamReader) -> tuple[str, bool]:
    captured = bytearray()
    total = 0
    truncated = False
    while True:
        chunk = await stream.read(4_096)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_STREAM_BYTES:
            raise _OutputLimitExceeded
        remaining = _MAX_CAPTURE_BYTES - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
    decoded = captured.decode("utf-8", errors="replace")
    return _redact_sensitive_text(decoded), truncated


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def _run_sandboxed_command(
    workspace: Path,
    command: str,
    timeout_seconds: int,
) -> CommandResponse:
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *_sandbox_argv(workspace, command, timeout_seconds),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        await _kill_process_group(process)
        raise RuntimeError("Command broker streams are unavailable")

    stdout_task = asyncio.create_task(_read_bounded(process.stdout))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr))
    try:
        async with asyncio.timeout(timeout_seconds):
            exit_code, stdout_result, stderr_result = await asyncio.gather(
                process.wait(),
                stdout_task,
                stderr_task,
            )
    except TimeoutError:
        await _kill_process_group(process)
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        return CommandResponse(
            status="timed_out",
            durationMs=int((time.monotonic() - started) * 1_000),
            stderr="The workspace command exceeded its execution deadline.",
        )
    except _OutputLimitExceeded:
        await _kill_process_group(process)
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        return CommandResponse(
            status="output_limited",
            durationMs=int((time.monotonic() - started) * 1_000),
            stderr="The workspace command exceeded its safe output limit.",
            outputTruncated=True,
        )
    except asyncio.CancelledError:
        await _kill_process_group(process)
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise

    stdout, stdout_truncated = stdout_result
    stderr, stderr_truncated = stderr_result
    return CommandResponse(
        status="completed" if exit_code == 0 else "failed",
        exitCode=exit_code,
        stdout=stdout,
        stderr=stderr,
        durationMs=int((time.monotonic() - started) * 1_000),
        outputTruncated=stdout_truncated or stderr_truncated,
    )


def _health_probe_command() -> str:
    network_probe = (
        "import errno,socket,sys\n"
        "try:\n"
        "    socket.socket()\n"
        "except OSError as exc:\n"
        "    sys.exit(0 if exc.errno == errno.EAFNOSUPPORT else 1)\n"
        "sys.exit(1)"
    )
    return (
        'test "$PWD" = /workspace'
        " && test ! -e /codex-auth"
        " && test ! -e /etc/lunarengine-prefect.env"
        f" && /usr/bin/python3 -c {shlex.quote(network_probe)}"
        f" && printf {_HEALTH_MARKER}"
    )


async def _verify_sandbox_execution() -> None:
    global _last_health_success_monotonic

    now = time.monotonic()
    if (
        _last_health_success_monotonic > 0
        and now - _last_health_success_monotonic < _HEALTH_PROBE_TTL_SECONDS
    ):
        return
    async with _HEALTH_PROBE_LOCK:
        now = time.monotonic()
        if (
            _last_health_success_monotonic > 0
            and now - _last_health_success_monotonic < _HEALTH_PROBE_TTL_SECONDS
        ):
            return
        workspace = _prepare_workspace(
            _HEALTH_WORKSPACE_ID,
            allow_health_workspace=True,
        )
        try:
            async with _COMMAND_CONCURRENCY:
                result = await _run_sandboxed_command(
                    workspace,
                    _health_probe_command(),
                    5,
                )
        except Exception as exc:
            logger.warning(
                "Command sandbox executable health probe failed error=%s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail="Command sandbox is unavailable",
            ) from exc
        if (
            result.status != "completed"
            or result.exitCode != 0
            or result.stdout != _HEALTH_MARKER
            or result.stderr
        ):
            logger.warning(
                "Command sandbox executable health probe rejected status=%s exit_code=%s duration_ms=%s",
                result.status,
                result.exitCode,
                result.durationMs,
            )
            raise HTTPException(
                status_code=503,
                detail="Command sandbox is unavailable",
            )
        os.utime(workspace, None)
        _last_health_success_monotonic = time.monotonic()


@app.get("/live", dependencies=[Depends(_require_token)])
async def live() -> dict[str, str]:
    if not _bubblewrap_binary().is_file() or not _prlimit_binary().is_file():
        raise HTTPException(status_code=503, detail="Command sandbox is unavailable")
    if not _workspace_root().is_dir():
        raise HTTPException(status_code=503, detail="Command workspace is unavailable")
    await _verify_sandbox_execution()
    return {
        "status": "ok",
        "sandbox": "bubblewrap",
        "verification": "executable",
    }


@app.post(
    "/v1/command",
    response_model=CommandResponse,
    dependencies=[Depends(_require_token)],
)
async def run_command(request: CommandRequest) -> CommandResponse:
    workspace = _prepare_workspace(request.workspaceId)
    command = request.command.strip()
    if not command or "\x00" in command:
        raise HTTPException(status_code=400, detail="Invalid workspace command")
    command_fingerprint = hashlib.sha256(command.encode()).hexdigest()[:12]
    workspace_fingerprint = hashlib.sha256(request.workspaceId.encode()).hexdigest()[:12]
    async with _COMMAND_CONCURRENCY:
        result = await _run_sandboxed_command(
            workspace,
            command,
            request.timeoutSeconds,
        )
    os.utime(workspace, None)
    logger.info(
        "Workspace command completed workspace=%s command=%s status=%s exit_code=%s duration_ms=%s",
        workspace_fingerprint,
        command_fingerprint,
        result.status,
        result.exitCode,
        result.durationMs,
    )
    return result
