from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)
app = FastAPI(title="LunarAgent command broker", docs_url=None, redoc_url=None)

_WORKSPACE_ID = re.compile(r"^[a-f0-9]{24}$")
_WORKSPACE_CLEANUP_PREFIX = ".cleanup-"
_MAX_CAPTURE_BYTES = 16_000
_MAX_OUTPUT_BYTES = 128_000
_COMMAND_CONCURRENCY = asyncio.Semaphore(2)
_HEALTH_PROBE_LOCK = asyncio.Lock()
_WORKSPACE_STATE_LOCK = asyncio.Lock()
_WORKSPACE_CLEANUP_LOCK = asyncio.Lock()
_HEALTH_PROBE_TTL_SECONDS = 30.0
_HEALTH_WORKSPACE_ID = "0" * 24
_HEALTH_MARKER = "LUNAR_AGENT_COMMAND_SANDBOX_READY"
_last_health_success_monotonic = 0.0
_last_workspace_cleanup_monotonic = 0.0
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


@dataclass
class _OutputBudget:
    total: int = 0

    def consume(self, size: int) -> None:
        self.total += max(0, int(size))
        if self.total > _MAX_OUTPUT_BYTES:
            raise _OutputLimitExceeded


@dataclass
class _WorkspaceState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


_workspace_states: dict[str, _WorkspaceState] = {}


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


def _bounded_environment_seconds(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(str(os.getenv(name) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _workspace_ttl_seconds() -> int:
    return _bounded_environment_seconds(
        "LUNAR_AGENT_COMMAND_WORKSPACE_TTL_SECONDS",
        default=86_400,
        minimum=3_600,
        maximum=604_800,
    )


def _workspace_cleanup_interval_seconds() -> int:
    return _bounded_environment_seconds(
        "LUNAR_AGENT_COMMAND_WORKSPACE_CLEANUP_INTERVAL_SECONDS",
        default=300,
        minimum=30,
        maximum=3_600,
    )


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


async def _cleanup_stale_workspaces(*, force: bool = False) -> int:
    global _last_workspace_cleanup_monotonic

    now_monotonic = time.monotonic()
    if (
        not force
        and _last_workspace_cleanup_monotonic > 0
        and now_monotonic - _last_workspace_cleanup_monotonic
        < _workspace_cleanup_interval_seconds()
    ):
        return 0

    async with _WORKSPACE_CLEANUP_LOCK:
        now_monotonic = time.monotonic()
        if (
            not force
            and _last_workspace_cleanup_monotonic > 0
            and now_monotonic - _last_workspace_cleanup_monotonic
            < _workspace_cleanup_interval_seconds()
        ):
            return 0

        root = _workspace_root()
        if not root.is_dir():
            return 0
        cleanup_paths: list[Path] = []
        cutoff = time.time() - _workspace_ttl_seconds()
        async with _WORKSPACE_STATE_LOCK:
            active_workspace_ids = set(_workspace_states)
            try:
                candidates = list(root.iterdir())
            except OSError:
                return 0
            for candidate in candidates:
                name = candidate.name
                if name.startswith(_WORKSPACE_CLEANUP_PREFIX):
                    if candidate.is_symlink():
                        continue
                    cleanup_paths.append(candidate)
                    continue
                if (
                    name == _HEALTH_WORKSPACE_ID
                    or name in active_workspace_ids
                    or not _WORKSPACE_ID.fullmatch(name)
                    or candidate.is_symlink()
                ):
                    continue
                try:
                    stat_result = candidate.stat()
                    resolved = candidate.resolve(strict=True)
                except OSError:
                    continue
                if (
                    not candidate.is_dir()
                    or resolved.parent != root
                    or stat_result.st_mtime >= cutoff
                ):
                    continue
                tombstone = root / (
                    f"{_WORKSPACE_CLEANUP_PREFIX}{name}-{secrets.token_hex(4)}"
                )
                try:
                    candidate.rename(tombstone)
                except OSError:
                    continue
                cleanup_paths.append(tombstone)
            _last_workspace_cleanup_monotonic = now_monotonic

        removed = 0
        for cleanup_path in cleanup_paths:
            try:
                if cleanup_path.is_dir() and not cleanup_path.is_symlink():
                    shutil.rmtree(cleanup_path)
                else:
                    cleanup_path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                logger.warning(
                    "Command workspace cleanup failed workspace=%s",
                    hashlib.sha256(cleanup_path.name.encode()).hexdigest()[:12],
                )
        if removed:
            logger.info("Removed stale command workspaces count=%s", removed)
        return removed


@asynccontextmanager
async def _workspace_execution(
    workspace_id: str,
    *,
    allow_health_workspace: bool = False,
) -> AsyncIterator[Path]:
    async with _WORKSPACE_STATE_LOCK:
        state = _workspace_states.setdefault(workspace_id, _WorkspaceState())
        state.users += 1
        try:
            workspace = _prepare_workspace(
                workspace_id,
                allow_health_workspace=allow_health_workspace,
            )
        except Exception:
            state.users -= 1
            if state.users == 0:
                _workspace_states.pop(workspace_id, None)
            raise
    try:
        async with state.lock:
            yield workspace
    finally:
        async with _WORKSPACE_STATE_LOCK:
            state.users -= 1
            if state.users == 0:
                _workspace_states.pop(workspace_id, None)


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


async def _read_bounded(
    stream: asyncio.StreamReader,
    output_budget: _OutputBudget,
) -> tuple[str, bool]:
    captured = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(4_096)
        if not chunk:
            break
        output_budget.consume(len(chunk))
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


async def _stop_command_process(
    process: asyncio.subprocess.Process | None,
    *stream_tasks: asyncio.Task[tuple[str, bool]] | None,
) -> None:
    if process is not None:
        await _kill_process_group(process)
    pending_tasks = [task for task in stream_tasks if task is not None]
    for task in pending_tasks:
        task.cancel()
    await asyncio.gather(*pending_tasks, return_exceptions=True)


async def _run_sandboxed_command(
    workspace: Path,
    command: str,
    timeout_seconds: float,
) -> CommandResponse:
    started = time.monotonic()
    process: asyncio.subprocess.Process | None = None
    stdout_task: asyncio.Task[tuple[str, bool]] | None = None
    stderr_task: asyncio.Task[tuple[str, bool]] | None = None
    try:
        async with asyncio.timeout(timeout_seconds):
            process = await asyncio.create_subprocess_exec(
                *_sandbox_argv(workspace, command, timeout_seconds),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            if process.stdout is None or process.stderr is None:
                raise RuntimeError("Command broker streams are unavailable")
            output_budget = _OutputBudget()
            stdout_task = asyncio.create_task(
                _read_bounded(process.stdout, output_budget)
            )
            stderr_task = asyncio.create_task(
                _read_bounded(process.stderr, output_budget)
            )
            exit_code, stdout_result, stderr_result = await asyncio.gather(
                process.wait(),
                stdout_task,
                stderr_task,
            )
    except TimeoutError:
        await _stop_command_process(process, stdout_task, stderr_task)
        return CommandResponse(
            status="timed_out",
            durationMs=int((time.monotonic() - started) * 1_000),
            stderr="The workspace command exceeded its execution deadline.",
        )
    except _OutputLimitExceeded:
        await _stop_command_process(process, stdout_task, stderr_task)
        return CommandResponse(
            status="output_limited",
            durationMs=int((time.monotonic() - started) * 1_000),
            stderr="The workspace command exceeded its safe output limit.",
            outputTruncated=True,
        )
    except asyncio.CancelledError:
        await _stop_command_process(process, stdout_task, stderr_task)
        raise
    except Exception:
        await _stop_command_process(process, stdout_task, stderr_task)
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
        try:
            async with _workspace_execution(
                _HEALTH_WORKSPACE_ID,
                allow_health_workspace=True,
            ) as workspace:
                # Readiness must remain executable under the supported load of
                # two user commands. The dedicated health lock bounds this to
                # one additional short probe without consuming a user slot.
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
    await _cleanup_stale_workspaces()
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
    command = request.command.strip()
    if not command or "\x00" in command:
        raise HTTPException(status_code=400, detail="Invalid workspace command")
    command_fingerprint = hashlib.sha256(command.encode()).hexdigest()[:12]
    workspace_fingerprint = hashlib.sha256(request.workspaceId.encode()).hexdigest()[:12]
    await _cleanup_stale_workspaces()
    started = time.monotonic()
    try:
        async with asyncio.timeout(request.timeoutSeconds):
            async with _workspace_execution(request.workspaceId) as workspace:
                async with _COMMAND_CONCURRENCY:
                    remaining_seconds = max(
                        0.05,
                        request.timeoutSeconds - (time.monotonic() - started),
                    )
                    result = await _run_sandboxed_command(
                        workspace,
                        command,
                        remaining_seconds,
                    )
                os.utime(workspace, None)
    except TimeoutError:
        result = CommandResponse(
            status="timed_out",
            durationMs=int((time.monotonic() - started) * 1_000),
            stderr="The workspace command exceeded its execution deadline.",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Workspace command unavailable workspace=%s command=%s error=%s",
            workspace_fingerprint,
            command_fingerprint,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Command sandbox is unavailable",
        ) from exc
    logger.info(
        "Workspace command completed workspace=%s command=%s status=%s exit_code=%s duration_ms=%s",
        workspace_fingerprint,
        command_fingerprint,
        result.status,
        result.exitCode,
        result.durationMs,
    )
    return result
