from __future__ import annotations

import asyncio
import os
import stat
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lunar_agent import command_broker


WORKSPACE_ID = "a" * 24


def _configure_broker(tmp_path: Path, monkeypatch) -> tuple[TestClient, Path]:
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / WORKSPACE_ID
    workspace.mkdir(parents=True)
    bwrap = tmp_path / "bwrap"
    prlimit = tmp_path / "prlimit"
    bwrap.write_text("", encoding="utf-8")
    prlimit.write_text("", encoding="utf-8")
    monkeypatch.setenv("LUNAR_AGENT_COMMAND_BROKER_TOKEN", "broker-test-token")
    monkeypatch.setenv("LUNAR_AGENT_COMMAND_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("LUNAR_AGENT_BWRAP_BINARY", str(bwrap))
    monkeypatch.setenv("LUNAR_AGENT_PRLIMIT_BINARY", str(prlimit))
    monkeypatch.setattr(command_broker, "_last_health_success_monotonic", 0.0)
    monkeypatch.setattr(command_broker, "_last_workspace_cleanup_monotonic", 0.0)
    monkeypatch.setattr(command_broker, "_COMMAND_CONCURRENCY", asyncio.Semaphore(2))
    monkeypatch.setattr(command_broker, "_HEALTH_PROBE_LOCK", asyncio.Lock())
    monkeypatch.setattr(command_broker, "_WORKSPACE_STATE_LOCK", asyncio.Lock())
    monkeypatch.setattr(command_broker, "_WORKSPACE_CLEANUP_LOCK", asyncio.Lock())
    monkeypatch.setattr(command_broker, "_workspace_states", {})
    return TestClient(command_broker.app), workspace


def test_command_broker_fails_closed_without_authentication(tmp_path, monkeypatch):
    client, _workspace = _configure_broker(tmp_path, monkeypatch)

    response = client.post(
        "/v1/command",
        json={
            "workspaceId": WORKSPACE_ID,
            "command": "printf ok",
            "timeoutSeconds": 5,
        },
    )

    assert response.status_code == 401


def test_command_broker_uses_only_the_resolved_workspace_and_networkless_bwrap(
    tmp_path,
    monkeypatch,
):
    _client, workspace = _configure_broker(tmp_path, monkeypatch)

    argv = command_broker._sandbox_argv(workspace, "printf ok", 5)

    assert "--unshare-net" in argv
    assert "--nproc=256" in argv
    assert ["--bind", str(workspace), "/workspace"] == argv[
        argv.index("--bind") : argv.index("--bind") + 3
    ]
    assert "/codex-auth" not in argv
    assert "/root" not in argv
    assert "/etc" not in argv
    assert argv[-3:] == ["--norc", "-c", "printf ok"]


def test_command_broker_creates_an_opaque_workspace_lazily(tmp_path, monkeypatch):
    client, workspace = _configure_broker(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer broker-test-token"}
    workspace.rmdir()

    async def fake_run(actual_workspace, command, timeout_seconds):
        assert actual_workspace == workspace
        return command_broker.CommandResponse(
            status="completed",
            exitCode=0,
            stdout="ok",
            durationMs=4,
        )

    monkeypatch.setattr(command_broker, "_run_sandboxed_command", fake_run)
    response = client.post(
        "/v1/command",
        headers=headers,
        json={
            "workspaceId": WORKSPACE_ID,
            "command": "printf ok",
            "timeoutSeconds": 5,
        },
    )

    assert response.status_code == 200
    assert workspace.is_dir()
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700


def test_command_broker_rejects_reserved_traversing_or_linked_workspaces(
    tmp_path,
    monkeypatch,
):
    client, _workspace = _configure_broker(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer broker-test-token"}

    reserved = client.post(
        "/v1/command",
        headers=headers,
        json={
            "workspaceId": command_broker._HEALTH_WORKSPACE_ID,
            "command": "printf ok",
            "timeoutSeconds": 5,
        },
    )
    malformed = client.post(
        "/v1/command",
        headers=headers,
        json={
            "workspaceId": "../outside-workspace",
            "command": "printf ok",
            "timeoutSeconds": 5,
        },
    )
    external = tmp_path / "external"
    external.mkdir()
    linked_id = "b" * 24
    (tmp_path / "workspaces" / linked_id).symlink_to(external, target_is_directory=True)
    linked = client.post(
        "/v1/command",
        headers=headers,
        json={
            "workspaceId": linked_id,
            "command": "printf ok",
            "timeoutSeconds": 5,
        },
    )

    assert reserved.status_code == 400
    assert malformed.status_code == 422
    assert linked.status_code == 400


def test_command_broker_live_proves_executable_isolation_and_caches_success(
    tmp_path,
    monkeypatch,
):
    client, _workspace = _configure_broker(tmp_path, monkeypatch)
    calls = []

    async def fake_run(workspace, command, timeout_seconds):
        calls.append((workspace, command, timeout_seconds))
        return command_broker.CommandResponse(
            status="completed",
            exitCode=0,
            stdout=command_broker._HEALTH_MARKER,
            durationMs=8,
        )

    monkeypatch.setattr(command_broker, "_run_sandboxed_command", fake_run)
    headers = {"Authorization": "Bearer broker-test-token"}

    first = client.get("/live", headers=headers)
    second = client.get("/live", headers=headers)

    assert first.status_code == 200
    assert first.json() == {
        "status": "ok",
        "sandbox": "bubblewrap",
        "verification": "executable",
    }
    assert second.status_code == 200
    assert len(calls) == 1
    workspace, command, timeout_seconds = calls[0]
    assert workspace.name == command_broker._HEALTH_WORKSPACE_ID
    assert timeout_seconds == 5
    assert 'test "$PWD" = /workspace' in command
    assert "test ! -e /codex-auth" in command
    assert "test ! -e /etc/lunarengine-prefect.env" in command
    assert "errno.EAFNOSUPPORT" in command


def test_command_broker_live_fails_closed_on_unexecutable_sandbox(
    tmp_path,
    monkeypatch,
):
    client, _workspace = _configure_broker(tmp_path, monkeypatch)

    async def fake_run(workspace, command, timeout_seconds):
        return command_broker.CommandResponse(
            status="failed",
            exitCode=1,
            stderr="private sandbox detail",
            durationMs=9,
        )

    monkeypatch.setattr(command_broker, "_run_sandboxed_command", fake_run)
    response = client.get(
        "/live",
        headers={"Authorization": "Bearer broker-test-token"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Command sandbox is unavailable"}
    assert "private sandbox detail" not in response.text


def test_command_broker_live_probe_is_not_starved_by_two_user_commands(
    tmp_path,
    monkeypatch,
):
    _client, _workspace = _configure_broker(tmp_path, monkeypatch)
    calls = 0

    async def fake_run(workspace, command, timeout_seconds):
        nonlocal calls
        calls += 1
        return command_broker.CommandResponse(
            status="completed",
            exitCode=0,
            stdout=command_broker._HEALTH_MARKER,
            durationMs=5,
        )

    monkeypatch.setattr(command_broker, "_run_sandboxed_command", fake_run)

    async def exercise() -> None:
        await command_broker._COMMAND_CONCURRENCY.acquire()
        await command_broker._COMMAND_CONCURRENCY.acquire()
        try:
            await asyncio.wait_for(
                command_broker._verify_sandbox_execution(),
                timeout=0.25,
            )
        finally:
            command_broker._COMMAND_CONCURRENCY.release()
            command_broker._COMMAND_CONCURRENCY.release()

    asyncio.run(exercise())

    assert calls == 1


def test_command_broker_returns_only_bounded_sandbox_result(tmp_path, monkeypatch):
    client, workspace = _configure_broker(tmp_path, monkeypatch)

    async def fake_run(actual_workspace, command, timeout_seconds):
        assert actual_workspace == workspace
        assert command == "printf ok"
        assert 0 < timeout_seconds <= 5
        return command_broker.CommandResponse(
            status="completed",
            exitCode=0,
            stdout="ok",
            durationMs=12,
        )

    monkeypatch.setattr(command_broker, "_run_sandboxed_command", fake_run)
    response = client.post(
        "/v1/command",
        headers={"Authorization": "Bearer broker-test-token"},
        json={
            "workspaceId": WORKSPACE_ID,
            "command": "printf ok",
            "timeoutSeconds": 5,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "completed",
        "exitCode": 0,
        "stdout": "ok",
        "stderr": "",
        "durationMs": 12,
        "outputTruncated": False,
    }


def test_command_broker_serializes_one_workspace_but_runs_two_investigations(
    tmp_path,
    monkeypatch,
):
    _client, _workspace = _configure_broker(tmp_path, monkeypatch)
    second_workspace_id = "b" * 24
    running_by_workspace: dict[str, int] = {}
    maximum_by_workspace: dict[str, int] = {}
    running_total = 0
    maximum_total = 0
    two_running = asyncio.Event()

    async def fake_run(workspace, command, timeout_seconds):
        nonlocal running_total, maximum_total
        assert 0 < timeout_seconds <= 5
        workspace_id = workspace.name
        running_by_workspace[workspace_id] = (
            running_by_workspace.get(workspace_id, 0) + 1
        )
        maximum_by_workspace[workspace_id] = max(
            maximum_by_workspace.get(workspace_id, 0),
            running_by_workspace[workspace_id],
        )
        running_total += 1
        maximum_total = max(maximum_total, running_total)
        if running_total == 2:
            two_running.set()
        await asyncio.wait_for(two_running.wait(), timeout=1)
        await asyncio.sleep(0.01)
        running_by_workspace[workspace_id] -= 1
        running_total -= 1
        return command_broker.CommandResponse(
            status="completed",
            exitCode=0,
            stdout=command,
            durationMs=10,
        )

    monkeypatch.setattr(command_broker, "_run_sandboxed_command", fake_run)

    async def exercise() -> None:
        await asyncio.gather(
            command_broker.run_command(
                command_broker.CommandRequest(
                    workspaceId=WORKSPACE_ID,
                    command="first",
                    timeoutSeconds=5,
                )
            ),
            command_broker.run_command(
                command_broker.CommandRequest(
                    workspaceId=WORKSPACE_ID,
                    command="second",
                    timeoutSeconds=5,
                )
            ),
            command_broker.run_command(
                command_broker.CommandRequest(
                    workspaceId=second_workspace_id,
                    command="other-investigation",
                    timeoutSeconds=5,
                )
            ),
        )

    asyncio.run(exercise())

    assert maximum_by_workspace == {
        WORKSPACE_ID: 1,
        second_workspace_id: 1,
    }
    assert maximum_total == 2
    assert command_broker._workspace_states == {}


def test_command_broker_queue_wait_is_bounded_by_the_request_deadline(
    tmp_path,
    monkeypatch,
):
    _client, _workspace = _configure_broker(tmp_path, monkeypatch)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[str] = []

    async def fake_run(workspace, command, timeout_seconds):
        calls.append(command)
        if command == "first":
            first_started.set()
            await release_first.wait()
        return command_broker.CommandResponse(
            status="completed",
            exitCode=0,
            stdout=command,
            durationMs=10,
        )

    monkeypatch.setattr(command_broker, "_run_sandboxed_command", fake_run)

    async def exercise() -> tuple[command_broker.CommandResponse, float]:
        first = asyncio.create_task(
            command_broker.run_command(
                command_broker.CommandRequest(
                    workspaceId=WORKSPACE_ID,
                    command="first",
                    timeoutSeconds=5,
                )
            )
        )
        await asyncio.wait_for(first_started.wait(), timeout=1)
        started = time.monotonic()
        second = await command_broker.run_command(
            command_broker.CommandRequest(
                workspaceId=WORKSPACE_ID,
                command="queued",
                timeoutSeconds=1,
            )
        )
        elapsed = time.monotonic() - started
        release_first.set()
        await first
        return second, elapsed

    result, elapsed = asyncio.run(exercise())

    assert result.status == "timed_out"
    assert result.exitCode is None
    assert 0.8 <= elapsed < 3
    assert calls == ["first"]
    assert command_broker._workspace_states == {}


def test_command_broker_enforces_deadline_across_the_full_process_lifecycle(
    tmp_path,
    monkeypatch,
):
    _client, workspace = _configure_broker(tmp_path, monkeypatch)
    monkeypatch.setattr(
        command_broker,
        "_sandbox_argv",
        lambda _workspace, _command, _timeout: [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
    )

    started = time.monotonic()
    result = asyncio.run(
        command_broker._run_sandboxed_command(workspace, "sleep", 1)
    )

    assert result.status == "timed_out"
    assert result.exitCode is None
    assert result.outputTruncated is False
    assert time.monotonic() - started < 4


def test_command_broker_combines_stdout_and_stderr_for_the_output_limit(
    tmp_path,
    monkeypatch,
):
    _client, workspace = _configure_broker(tmp_path, monkeypatch)
    monkeypatch.setattr(command_broker, "_MAX_OUTPUT_BYTES", 10_000)
    script = (
        "import sys\n"
        "sys.stdout.write('a' * 6000)\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('b' * 6000)\n"
        "sys.stderr.flush()\n"
    )
    monkeypatch.setattr(
        command_broker,
        "_sandbox_argv",
        lambda _workspace, _command, _timeout: [
            sys.executable,
            "-c",
            script,
        ],
    )

    result = asyncio.run(
        command_broker._run_sandboxed_command(workspace, "emit", 5)
    )

    assert result.status == "output_limited"
    assert result.exitCode is None
    assert result.stdout == ""
    assert result.outputTruncated is True


def test_command_broker_cancellation_kills_and_reaps_the_process_group(
    tmp_path,
    monkeypatch,
):
    _client, workspace = _configure_broker(tmp_path, monkeypatch)
    pid_file = tmp_path / "command.pid"
    script = (
        "import os,pathlib,sys,time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setattr(
        command_broker,
        "_sandbox_argv",
        lambda _workspace, _command, _timeout: [
            sys.executable,
            "-c",
            script,
            str(pid_file),
        ],
    )

    async def exercise() -> int:
        task = asyncio.create_task(
            command_broker._run_sandboxed_command(workspace, "wait", 20)
        )
        for _attempt in range(100):
            if pid_file.is_file():
                break
            await asyncio.sleep(0.01)
        assert pid_file.is_file()
        pid = int(pid_file.read_text(encoding="utf-8"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pid

    pid = asyncio.run(exercise())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_command_broker_removes_only_inactive_expired_workspaces(
    tmp_path,
    monkeypatch,
):
    _client, workspace = _configure_broker(tmp_path, monkeypatch)
    root = workspace.parent
    stale = root / ("b" * 24)
    fresh = root / ("c" * 24)
    health = root / command_broker._HEALTH_WORKSPACE_ID
    stale.mkdir()
    fresh.mkdir()
    health.mkdir()
    (stale / "temporary.txt").write_text("remove", encoding="utf-8")
    tombstone = root / ".cleanup-prior-crash"
    tombstone.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    linked = root / ("d" * 24)
    linked.symlink_to(external, target_is_directory=True)
    expired = time.time() - 120
    for candidate in (workspace, stale, health):
        os.utime(candidate, (expired, expired))
    monkeypatch.setattr(command_broker, "_workspace_ttl_seconds", lambda: 60)

    async def exercise() -> int:
        async with command_broker._workspace_execution(WORKSPACE_ID):
            return await command_broker._cleanup_stale_workspaces(force=True)

    removed = asyncio.run(exercise())

    assert removed == 2
    assert workspace.is_dir()
    assert health.is_dir()
    assert fresh.is_dir()
    assert linked.is_symlink()
    assert external.is_dir()
    assert not stale.exists()
    assert not tombstone.exists()


def test_command_broker_returns_a_safe_error_when_the_sandbox_cannot_start(
    tmp_path,
    monkeypatch,
):
    client, _workspace = _configure_broker(tmp_path, monkeypatch)

    async def unavailable(*_args, **_kwargs):
        raise OSError("private process launch detail")

    monkeypatch.setattr(command_broker, "_run_sandboxed_command", unavailable)
    response = client.post(
        "/v1/command",
        headers={"Authorization": "Bearer broker-test-token"},
        json={
            "workspaceId": WORKSPACE_ID,
            "command": "printf ok",
            "timeoutSeconds": 5,
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Command sandbox is unavailable"}
    assert "private process launch detail" not in response.text


def test_command_broker_redacts_credentials_from_command_output():
    text = command_broker._redact_sensitive_text(
        "Authorization: Bearer super-secret-value\n"
        "api_key=sk-proj-abcdefghijklmnopqrstuvwxyz"
    )

    assert "super-secret-value" not in text
    assert "sk-proj-" not in text
    assert text.count("[redacted]") >= 2
