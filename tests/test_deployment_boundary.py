from __future__ import annotations

from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "unit_path",
    (
        REPOSITORY_ROOT / "deploy" / "lunar-agent.service",
        REPOSITORY_ROOT / "deploy" / "systemd" / "lunar-agent.service",
    ),
)
def test_agent_container_has_no_host_command_surface(unit_path: Path) -> None:
    unit = unit_path.read_text(encoding="utf-8")

    assert (
        "Environment=LUNAR_AGENT_CODEX_WORKSPACE_ROOT="
        "/tmp/lunar-agent-codex-workspaces"
    ) in unit
    assert "--volume /var/lib/lunar-agent/workspaces" not in unit
    assert "lunar-agent-command-broker" not in unit
    assert "LUNAR_AGENT_COMMAND_BROKER" not in unit
    assert "--read-only" in unit
    assert "--security-opt no-new-privileges" in unit
    assert "--cap-drop ALL" in unit
    assert "--tmpfs /tmp:rw,noexec,nosuid,nodev,size=512m" in unit
    assert "Environment=LUNAR_AGENT_AREA_RISK_PROVIDER_MODE=chatgpt-account" in unit
    assert "--env LUNAR_AGENT_AREA_RISK_PROVIDER_MODE" in unit
    assert "Environment=LUNAR_AGENT_AREA_RISK_ACCOUNT_ENABLED=true" in unit
    assert "Environment=LUNAR_AGENT_AREA_RISK_CODEX_MODEL=gpt-5.6-sol" in unit
    assert "--env LUNAR_AGENT_AREA_RISK_CODEX_TIMEOUT" in unit


def test_host_command_broker_is_not_shipped() -> None:
    forbidden_paths = (
        REPOSITORY_ROOT / "deploy" / "lunar-agent-command-broker.service",
        REPOSITORY_ROOT / "src" / "lunar_agent" / "command_broker.py",
    )

    assert all(not path.exists() for path in forbidden_paths)


def test_checked_execution_policy_is_read_only() -> None:
    policy = (
        REPOSITORY_ROOT / "codex_runtime" / "execution_policy.json"
    ).read_text(encoding="utf-8")

    assert '"mode": "read-only-no-host-exec"' in policy
    assert '"sandboxMode": "read-only"' in policy
    assert '"networkAccessEnabled": false' in policy
    assert '"hostCommands": false' in policy
    assert '"fileWrites": false' in policy


def test_area_risk_codex_runner_is_shipped_in_the_container_context() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")
    runner = REPOSITORY_ROOT / "codex_runtime" / "area_risk_runner.mjs"

    assert runner.is_file()
    assert "COPY codex_runtime ./codex_runtime" in dockerfile
