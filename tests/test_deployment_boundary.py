from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_agent_container_cannot_mount_host_command_workspaces() -> None:
    unit = (REPOSITORY_ROOT / "deploy" / "lunar-agent.service").read_text(
        encoding="utf-8"
    )

    assert (
        "Environment=LUNAR_AGENT_CODEX_WORKSPACE_ROOT="
        "/tmp/lunar-agent-codex-workspaces"
    ) in unit
    assert "--volume /var/lib/lunar-agent/workspaces" not in unit
    assert (
        "--volume /run/lunar-agent-command-broker:"
        "/run/lunar-agent-command-broker:ro"
    ) in unit
    assert "Requires=lunar-agent-command-broker.service" in unit
    assert "BindsTo=lunar-agent-command-broker.service" in unit
    assert "PartOf=lunar-agent-command-broker.service" in unit


def test_broker_owns_the_only_host_command_workspace_mount() -> None:
    broker_unit = (
        REPOSITORY_ROOT / "deploy" / "lunar-agent-command-broker.service"
    ).read_text(encoding="utf-8")

    assert (
        "ReadWritePaths=/var/lib/lunar-agent/workspaces "
        "/run/lunar-agent-command-broker"
    ) in broker_unit
    assert (
        "Environment=PYTHONPATH=/opt/lunar-agent-command-broker/current/src"
    ) in broker_unit
