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


@pytest.mark.docker
def test_docker_installs_test_dependency_groups(tmp_path: Path) -> None:
    """PEP 735 dependency groups (attrs, click, ...) hold test-only dependencies that an
    extras-based install can't see; without them pytest dies collecting `import hypothesis`."""
    if not docker_available():
        pytest.skip("docker unavailable")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\ndependencies = []\n'
        '[dependency-groups]\ntests = ["iniconfig"]\ndocs = ["not-a-real-pkg-xyz"]\n'
    )
    (tmp_path / "test_demo.py").write_text(
        "import iniconfig\n\n\ndef test_group_dependency_is_importable():\n"
        "    assert iniconfig.__name__ == 'iniconfig'\n"
    )
    sb = DockerSandbox("python:3.12-slim")
    py = sb.python(tmp_path, tmp_path)
    res = sb.run([*py, "-m", "pytest", "-q", "-p", "no:cacheprovider"], tmp_path, timeout=120)
    assert res.ok, res.combined[-800:]


@pytest.mark.docker
def test_docker_installs_a_tests_extra_even_when_there_is_no_test_extra(tmp_path: Path) -> None:
    """`pip install -e '.[test]'` warns and succeeds when the extra doesn't exist, so a
    project whose extra is called `tests` used to get no test dependencies at all."""
    if not docker_available():
        pytest.skip("docker unavailable")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\ndependencies = []\n'
        '[project.optional-dependencies]\ntests = ["iniconfig"]\ndocs = ["not-a-real-pkg-xyz"]\n'
    )
    (tmp_path / "test_demo.py").write_text(
        "import iniconfig\n\n\ndef test_ok():\n    assert iniconfig\n"
    )
    sb = DockerSandbox("python:3.12-slim")
    res = sb.run([*sb.python(tmp_path, tmp_path), "-m", "pytest", "-q"], tmp_path, timeout=120)
    assert res.ok, res.combined[-800:]


@pytest.mark.docker
def test_docker_installs_vcs_versioned_projects(tmp_path: Path) -> None:
    """attrs/click/httpx-style projects derive their version from git, which doesn't exist
    inside the container. Without a pretend version their editable install fails outright."""
    if not docker_available():
        pytest.skip("docker unavailable")
    pkg = tmp_path / "vcsdemo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("X = 1\n")
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=64", "setuptools-scm>=8"]\n'
        'build-backend = "setuptools.build_meta"\n'
        '[project]\nname = "vcsdemo"\ndynamic = ["version"]\n'
        '[tool.setuptools_scm]\n[tool.setuptools]\npackages = ["vcsdemo"]\n'
    )
    (tmp_path / "test_demo.py").write_text(
        "import vcsdemo\n\n\ndef test_ok():\n    assert vcsdemo.X == 1\n"
    )
    sb = DockerSandbox("python:3.12-slim")
    res = sb.run([*sb.python(tmp_path, tmp_path), "-m", "pytest", "-q"], tmp_path, timeout=120)
    assert res.ok, res.combined[-800:]
