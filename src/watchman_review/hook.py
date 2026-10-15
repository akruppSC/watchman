"""Install a git pre-push hook that runs Watchman on the commits being pushed.

The hook blocks a push only when the review's verdict is BLOCK. Tool errors, an incomplete
review, or a missing base never block a push: a broken reviewer must not become a reason you
can't ship. Bypass it with `git push --no-verify` or `WATCHMAN_SKIP=1 git push`.
"""

from __future__ import annotations

import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from watchman_review.gitutil import run_git

HOOK_NAME = "pre-push"
HOOK_MARKER = "# watchman-review pre-push hook"
# Layer 1 minus the test suite: fast enough to run on every push.
FAST_CHECKS = "secrets,imports,hygiene,scope,ruff,mypy"
ALL_CHECKS = FAST_CHECKS + ",pytest"


class HookError(Exception):
    pass


@dataclass
class HookStatus:
    path: Path
    installed: bool  # ours
    foreign: bool  # some other pre-push hook is in the way


def hook_path(repo: Path) -> Path:
    """Where git will look for pre-push (honors core.hooksPath and worktrees)."""
    raw = run_git(repo, "rev-parse", "--git-path", f"hooks/{HOOK_NAME}").strip()
    p = Path(raw)
    return p if p.is_absolute() else (repo / p)


def render_hook(*, python: str, mode: str, checks: str, extra_args: list[str]) -> str:
    argv = [python, "-m", "watchman_review", "review", "--mode", mode, "--checks", checks]
    cmd = " ".join(shlex.quote(a) for a in [*argv, "--fail-on", "block", *extra_args])
    return f"""#!/bin/sh
{HOOK_MARKER}
# Installed by `watchman hook install`. Remove with `watchman hook uninstall`.
# Skip once: `git push --no-verify`, or `WATCHMAN_SKIP=1 git push`.
[ -n "$WATCHMAN_SKIP" ] && exit 0
# git exports repo-location variables to hooks; the review makes its own worktrees.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX
zero=0000000000000000000000000000000000000000
status=0

default_base() {{
  for ref in "$(git symbolic-ref --short -q refs/remotes/origin/HEAD)" \\
             origin/main origin/master main master; do
    [ -n "$ref" ] || continue
    if git rev-parse -q --verify "$ref^{{commit}}" >/dev/null 2>&1; then
      echo "$ref"
      return
    fi
  done
}}

while read -r local_ref local_sha remote_ref remote_sha; do
  case "$local_ref" in refs/heads/*) ;; *) continue ;; esac   # branches only, not tags
  [ "$local_sha" = "$zero" ] && continue                      # deleting a branch
  if [ "$remote_sha" != "$zero" ] && git cat-file -e "$remote_sha^{{commit}}" 2>/dev/null; then
    base="$remote_sha"                                        # review only the new commits
  else
    base="$(default_base)"                                    # new branch: against main
  fi
  if [ -z "$base" ]; then
    echo "watchman: no base branch found; skipping review of $local_ref" >&2
    continue
  fi
  echo "watchman: reviewing $local_ref against $base ..." >&2
  {cmd} --head "$local_sha" --base "$base" </dev/null
  rc=$?
  if [ "$rc" -eq 1 ]; then
    echo "watchman: push blocked. Fix the findings, or bypass with 'git push --no-verify'." >&2
    status=1
  elif [ "$rc" -ne 0 ]; then
    echo "watchman: review could not complete (exit $rc); allowing the push." >&2
  fi
done
exit $status
"""


def status(repo: Path) -> HookStatus:
    path = hook_path(repo)
    if not path.exists():
        return HookStatus(path, installed=False, foreign=False)
    ours = HOOK_MARKER in path.read_text(errors="replace")
    return HookStatus(path, installed=ours, foreign=not ours)


def install(
    repo: Path, *, mode: str = "deterministic", with_tests: bool = False, force: bool = False,
    extra_args: list[str] | None = None, python: str | None = None,
) -> Path:  # fmt: skip
    st = status(repo)
    if st.foreign and not force:
        raise HookError(
            f"{st.path} already exists and isn't a Watchman hook. Re-run with --force to "
            "replace it (the old one is kept as pre-push.before-watchman)."
        )
    if st.foreign:
        st.path.rename(st.path.with_name(HOOK_NAME + ".before-watchman"))
    st.path.parent.mkdir(parents=True, exist_ok=True)
    st.path.write_text(
        render_hook(
            python=python or sys.executable,
            mode=mode,
            checks=ALL_CHECKS if with_tests else FAST_CHECKS,
            extra_args=extra_args or [],
        )
    )
    st.path.chmod(st.path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return st.path


def uninstall(repo: Path) -> Path | None:
    """Remove our hook (restoring a hook we displaced). Returns the path, or None if absent."""
    st = status(repo)
    if st.foreign:
        raise HookError(f"{st.path} isn't a Watchman hook; not touching it")
    if not st.installed:
        return None
    st.path.unlink()
    backup = st.path.with_name(HOOK_NAME + ".before-watchman")
    if backup.exists():
        backup.rename(st.path)
    return st.path
