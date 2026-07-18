from __future__ import annotations

import stat
from pathlib import Path

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


def test_command_broker_returns_only_bounded_sandbox_result(tmp_path, monkeypatch):
    client, workspace = _configure_broker(tmp_path, monkeypatch)

    async def fake_run(actual_workspace, command, timeout_seconds):
        assert actual_workspace == workspace
        assert command == "printf ok"
        assert timeout_seconds == 5
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


def test_command_broker_redacts_credentials_from_command_output():
    text = command_broker._redact_sensitive_text(
        "Authorization: Bearer super-secret-value\n"
        "api_key=sk-proj-abcdefghijklmnopqrstuvwxyz"
    )

    assert "super-secret-value" not in text
    assert "sk-proj-" not in text
    assert text.count("[redacted]") >= 2
