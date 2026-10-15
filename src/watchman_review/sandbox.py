"""Execution sandboxes for running the target repo's tests and linters.

The code under review is untrusted. Whatever sandbox is used:
  * the environment is scrubbed (no API keys or tokens reach the child process),
  * there is a hard timeout that kills the whole process group / container,
  * output is truncated,
  * the network is disabled where possible (always in Docker; via `unshare -rn` in
    subprocess mode when unprivileged user namespaces are available).
"""

from __future__ import annotations

import contextlib
import functools
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from watchman_review.config import SandboxKind

MAX_OUTPUT_BYTES = 200_000
_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TERM", "TZ")


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def combined(self) -> str:
        return (self.stdout + ("\n" + self.stderr if self.stderr else "")).strip()


def _truncate(s: str) -> str:
    if len(s) <= MAX_OUTPUT_BYTES:
        return s
    half = MAX_OUTPUT_BYTES // 2
    return s[:half] + f"\n... [{len(s) - MAX_OUTPUT_BYTES} chars truncated] ...\n" + s[-half:]


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["NO_COLOR"] = "1"
    env.update(extra or {})
    return env


def _run_process(
    argv: list[str], cwd: Path, timeout: float, env: dict[str, str], on_timeout: str | None = None
) -> ExecResult:
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except OSError as e:
        return ExecResult(127, "", f"failed to start {argv[0]}: {e}", time.monotonic() - start)
    try:
        out, err = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        if on_timeout:
            subprocess.run(["docker", "kill", on_timeout], capture_output=True, check=False)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
    return ExecResult(
        returncode=proc.returncode if not timed_out else -9,
        stdout=_truncate(out or ""),
        stderr=_truncate(err or ""),
        duration_s=time.monotonic() - start,
        timed_out=timed_out,
    )


def run_static_tool(
    argv: list[str], cwd: Path, timeout: float = 120.0, env: dict[str, str] | None = None
) -> ExecResult:
    """Run a trusted static-analysis tool (ruff, mypy) that does not execute repo code."""
    return _run_process(argv, cwd, timeout, scrubbed_env({"HOME": str(cwd), **(env or {})}))


def find_repo_python(*roots: Path) -> Path | None:
    """Locate the target repo's virtualenv interpreter, if it has one."""
    for base in roots:
        for name in (".venv", "venv"):
            candidate = base / name / "bin" / "python"
            if candidate.exists():
                return candidate
    return None


class Sandbox(ABC):
    name: str
    network_isolated: bool

    @abstractmethod
    def run(
        self, argv: list[str], workdir: Path, timeout: float, env: dict[str, str] | None = None
    ) -> ExecResult:
        """Run argv with cwd=workdir. Paths in argv should be relative to workdir."""

    @abstractmethod
    def python(self, workdir: Path, repo_root: Path) -> list[str]:
        """Argv prefix for a Python interpreter that has the target repo's dependencies."""


@functools.cache
def _unshare_works() -> bool:
    if not shutil.which("unshare"):
        return False
    try:
        r = subprocess.run(["unshare", "-rn", "true"], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


class SubprocessSandbox(Sandbox):
    name = "subprocess"

    def __init__(self, *, isolate_network: bool = True) -> None:
        self.network_isolated = isolate_network and _unshare_works()

    def run(
        self, argv: list[str], workdir: Path, timeout: float, env: dict[str, str] | None = None
    ) -> ExecResult:
        full = ["unshare", "-rn", *argv] if self.network_isolated else argv
        child_env = scrubbed_env({"HOME": str(workdir), **(env or {})})
        return _run_process(full, workdir, timeout, child_env)

    def python(self, workdir: Path, repo_root: Path) -> list[str]:
        found = find_repo_python(workdir, repo_root)
        return [str(found)] if found else [sys.executable]


@functools.cache
def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


# What to install so the project's tests can import their dependencies. Test-ish extras and
# PEP 735 dependency groups are preferred in this order; `dev` is the fallback because it is
# often much heavier (docs, build tools). pip exits 0 for an extra that doesn't exist, so
# asking for `.[test]` blindly can't be used to probe: read pyproject.toml instead.
_PROBE_PY = (
    "import tomllib; d = tomllib.load(open('pyproject.toml', 'rb'))\n"
    "pick = lambda names: next(([n] for t in (('test', 'tests', 'testing'), ('dev',)) "
    "for n in t if n in names), [])\n"
    "extras = pick(d.get('project', {}).get('optional-dependencies', {}))\n"
    "groups = pick(d.get('dependency-groups', {}))\n"
    "print(','.join(extras)); print(' '.join('--group ' + g for g in groups))"
)
GROUP_PROBE = f'python -c "{_PROBE_PY}"'


class DockerSandbox(Sandbox):
    """Runs each command in a fresh container with the workdir mounted at /work.

    Dependency installation is the only step that gets network access; it installs into
    /work/.wm-venv inside the (temporary) snapshot so later no-network runs can use it.
    """

    name = "docker"
    network_isolated = True
    VENV = "/work/.wm-venv"

    def __init__(self, image: str, *, memory: str = "2g", cpus: str = "2") -> None:
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self._prepared: set[Path] = set()

    def _docker_argv(
        self, argv: list[str], workdir: Path, *, network: bool, name: str
    ) -> list[str]:
        cmd = [
            "docker", "run", "--rm", "--name", name,
            "--network", "bridge" if network else "none",
            "--memory", self.memory, "--cpus", self.cpus, "--pids-limit", "512",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "NO_COLOR=1",
            # The snapshot's .git points at a host path that doesn't exist in the container, so
            # setuptools-scm/hatch-vcs projects (attrs, click, ...) can't derive a version and
            # their editable install fails before installing any dependency.
            "-e", "SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+watchman",
            "-v", f"{workdir.resolve()}:/work", "-w", "/work",
            self.image, *argv,
        ]  # fmt: skip
        return cmd

    def run(
        self, argv: list[str], workdir: Path, timeout: float, env: dict[str, str] | None = None
    ) -> ExecResult:
        name = f"watchman-{uuid.uuid4().hex[:12]}"
        cmd = self._docker_argv(argv, workdir, network=False, name=name)
        if env:
            idx = cmd.index("-v")
            for k, v in env.items():
                cmd[idx:idx] = ["-e", f"{k}={v}"]
        return _run_process(cmd, workdir, timeout, scrubbed_env(), on_timeout=name)

    def python(self, workdir: Path, repo_root: Path) -> list[str]:
        if workdir in self._prepared:
            return [f"{self.VENV}/bin/python"]
        script = "\n".join(
            [
                f"python -m venv {self.VENV} && . {self.VENV}/bin/activate || exit 1",
                "pip install -q -U pip pytest",
                "if [ -f pyproject.toml ]; then",
                f"  probe=$({GROUP_PROBE} 2>/dev/null)",
                '  extras=$(printf "%s" "$probe" | sed -n 1p)',
                '  groups=$(printf "%s" "$probe" | sed -n 2p)',
                "else extras=; groups=; fi",
                "if [ -f pyproject.toml ] || [ -f setup.py ]; then",
                '  if [ -n "$extras" ]; then',
                '    pip install -q -e ".[$extras]" || pip install -q -e .',
                "  else",
                "    pip install -q -e .",
                "  fi",
                "fi",
                '[ -n "$groups" ] && pip install -q $groups',
                "for r in requirements*.txt requirements/*.txt; do",
                '  [ -f "$r" ] && pip install -q -r "$r"',
                "done",
                "true",
            ]
        )
        name = f"watchman-setup-{uuid.uuid4().hex[:12]}"
        res = _run_process(
            self._docker_argv(["sh", "-c", script], workdir, network=True, name=name),
            workdir,
            900,
            scrubbed_env(),
            on_timeout=name,
        )
        if not res.ok:
            raise SandboxError(f"dependency install failed in docker: {res.combined[-1500:]}")
        self._prepared.add(workdir)
        return [f"{self.VENV}/bin/python"]


class SandboxError(Exception):
    pass


def choose_sandbox(kind: SandboxKind, docker_image: str) -> tuple[Sandbox, list[str]]:
    """Pick a sandbox. Returns (sandbox, warnings)."""
    warnings: list[str] = []
    if kind is SandboxKind.DOCKER or (kind is SandboxKind.AUTO and docker_available()):
        if not docker_available():
            raise SandboxError("sandbox = 'docker' but docker is not available")
        return DockerSandbox(docker_image), warnings
    sb = SubprocessSandbox()
    warnings.append(
        "Running target-repo tests in a local subprocess (Docker unavailable): "
        + ("network disabled via unshare." if sb.network_isolated else "NETWORK NOT ISOLATED.")
    )
    return sb, warnings
