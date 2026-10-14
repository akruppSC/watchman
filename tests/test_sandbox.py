from __future__ import annotations

import sys
from pathlib import Path

import pytest

from watchman_review.config import SandboxKind
from watchman_review.sandbox import (
    DockerSandbox,
    SubprocessSandbox,
    choose_sandbox,
    docker_available,
    scrubbed_env,
)


def test_scrubbed_env_drops_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    env = scrubbed_env()
    assert "OPENROUTER_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "PATH" in env


def test_subprocess_child_cannot_see_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    sb = SubprocessSandbox()
    code = "import os; print(os.environ.get('ANTHROPIC_API_KEY', 'absent'))"
    res = sb.run([sys.executable, "-c", code], tmp_path, timeout=20)
    assert res.ok, res.combined
    assert res.stdout.strip() == "absent"


def test_subprocess_timeout_kills(tmp_path: Path) -> None:
    sb = SubprocessSandbox(isolate_network=False)
    res = sb.run([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, timeout=0.5)
    assert res.timed_out
    assert not res.ok
    assert res.duration_s < 10


def test_subprocess_network_isolated_when_supported(tmp_path: Path) -> None:
    sb = SubprocessSandbox()
    if not sb.network_isolated:
        pytest.skip("unprivileged user namespaces unavailable")
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=2); print('connected')\n"
        "except OSError:\n"
        "    print('blocked')\n"
    )
    res = sb.run([sys.executable, "-c", code], tmp_path, timeout=20)
    assert res.stdout.strip() == "blocked"


def test_python_prefers_repo_venv(tmp_path: Path) -> None:
    venv_py = tmp_path / "repo" / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    sb = SubprocessSandbox(isolate_network=False)
    assert sb.python(tmp_path / "snap", tmp_path / "repo") == [str(venv_py)]


def test_choose_subprocess_warns() -> None:
    sb, warnings = choose_sandbox(SandboxKind.SUBPROCESS, "img")
    assert isinstance(sb, SubprocessSandbox)
    assert len(warnings) == 1


@pytest.mark.docker
def test_docker_has_no_network(tmp_path: Path) -> None:
    if not docker_available():
        pytest.skip("docker unavailable")
    sb = DockerSandbox("python:3.12-slim")
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=2); print('connected')\n"
        "except OSError:\n"
        "    print('blocked')\n"
    )
    res = sb.run(["python", "-c", code], tmp_path, timeout=120)
    assert res.stdout.strip() == "blocked", res.combined
