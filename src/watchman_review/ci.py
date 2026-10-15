"""GitHub CI integration: review a pull request and post the result as one updatable comment.

Design notes:
- The PR's title, body, and diff are untrusted. The title and body become the review's task
  text (already wrapped as untrusted data), and the comment is rendered through `md_safe`.
- Fork PRs get a read-only token and no secrets. The review then falls back to Layer 1 only,
  and a comment that can't be posted degrades to the job summary instead of failing the run.
- Run this on the `pull_request` event only. `pull_request_target` would hand a secret-bearing
  token to code from the PR; the action's docs say so explicitly.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from watchman_review.config import ConfigError, OverseerTrigger, Provider, resolve_api_key
from watchman_review.gitutil import GitError, repo_root, rev_parse
from watchman_review.report_md import MARKER, render_markdown
from watchman_review.review import ReviewError, ReviewOptions, load_review_config, run_review
from watchman_review.schema import ReviewMode, ReviewResult, Verdict

MAX_TASK_CHARS = 4_000
MAX_COMMENT_PAGES = 10
GITHUB_COMMENT_LIMIT = 65_000


class CiError(Exception):
    pass


@dataclass
class PullRequest:
    number: int
    title: str
    body: str
    base_sha: str
    head_sha: str
    head_repo: str | None  # owner/name of the head repository (differs for forks)
    base_repo: str | None

    @property
    def is_fork(self) -> bool:
        return bool(self.head_repo and self.base_repo and self.head_repo != self.base_repo)

    def task(self) -> str:
        text = self.title.strip()
        if self.body.strip():
            text += "\n\n" + self.body.strip()
        return text[:MAX_TASK_CHARS]


def load_pull_request(event_path: Path) -> PullRequest:
    try:
        event = json.loads(event_path.read_text())
        pr = event["pull_request"]
        return PullRequest(
            number=int(pr["number"]),
            title=str(pr.get("title") or ""),
            body=str(pr.get("body") or ""),
            base_sha=str(pr["base"]["sha"]),
            head_sha=str(pr["head"]["sha"]),
            head_repo=((pr["head"].get("repo") or {}).get("full_name")),
            base_repo=((pr["base"].get("repo") or {}).get("full_name")),
        )
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise CiError(f"could not read a pull_request event from {event_path}: {e}") from e


# --- comment upsert ---------------------------------------------------------


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def find_our_comment(
    http: httpx.Client, api_url: str, repo: str, pr: int, token: str
) -> dict[str, object] | None:
    """The existing Watchman comment, if any: carries our marker and was written by a bot.

    The author check matters: anyone can paste the marker into their own comment, and we
    must never try to take over a human's comment.
    """
    for page in range(1, MAX_COMMENT_PAGES + 1):
        resp = http.get(
            f"{api_url}/repos/{repo}/issues/{pr}/comments",
            params={"per_page": 100, "page": page},
            headers=_headers(token),
        )
        resp.raise_for_status()
        batch = resp.json()
        for c in batch:
            user = c.get("user") or {}
            if MARKER in (c.get("body") or "") and user.get("type") == "Bot":
                return dict(c)
        if len(batch) < 100:
            return None
    return None


def upsert_comment(
    http: httpx.Client, api_url: str, repo: str, pr: int, body: str, token: str
) -> tuple[str, str]:
    """Create or update the Watchman comment. Returns (action, html_url)."""
    body = body[:GITHUB_COMMENT_LIMIT]
    existing = find_our_comment(http, api_url, repo, pr, token)
    if existing is not None:
        resp = http.patch(
            f"{api_url}/repos/{repo}/issues/comments/{existing['id']}",
            json={"body": body},
            headers=_headers(token),
        )
        if resp.status_code < 400:
            return "updated", str(resp.json().get("html_url", ""))
        # Edit refused (for example it belongs to a different app): fall through and post.
    resp = http.post(
        f"{api_url}/repos/{repo}/issues/{pr}/comments", json={"body": body}, headers=_headers(token)
    )
    resp.raise_for_status()
    return "created", str(resp.json().get("html_url", ""))


# --- GitHub Actions plumbing ------------------------------------------------


def write_outputs(path: str | None, values: Mapping[str, str]) -> None:
    if not path:
        return
    with open(path, "a") as fh:
        for k, v in values.items():
            fh.write(f"{k}={v.replace(chr(10), ' ')}\n")


def append_summary(path: str | None, markdown: str) -> None:
    if path:
        with open(path, "a") as fh:
            fh.write(markdown + "\n")


# --- orchestration ----------------------------------------------------------


@dataclass
class CiOptions:
    mode: ReviewMode = ReviewMode.DETERMINISTIC
    model: str | None = None
    provider: Provider | None = None
    checks: list[str] | None = None
    base: str | None = None
    head: str | None = None
    deep: bool = False
    comment: bool = True
    strict: bool = False  # an incomplete review fails the job
    fail_on: str = "block"  # block | warn | never
    out_dir: Path | None = None


@dataclass
class CiOutcome:
    result: ReviewResult
    markdown: str
    json_path: Path
    markdown_path: Path
    comment_action: str | None
    comment_url: str | None
    exit_code: int
    notes: list[str]


def gate(verdict: Verdict, fail_on: str) -> bool:
    return fail_on != "never" and (
        verdict is Verdict.BLOCK or (verdict is Verdict.WARN and fail_on == "warn")
    )


def run_ci(
    opts: CiOptions,
    env: Mapping[str, str],
    *,
    cwd: Path,
    http: httpx.Client | None = None,
    base_options: ReviewOptions | None = None,
) -> CiOutcome:
    notes: list[str] = []
    repo_slug = env.get("GITHUB_REPOSITORY", "")
    event_name = env.get("GITHUB_EVENT_NAME", "")
    pr: PullRequest | None = None
    if event_name.startswith("pull_request") and env.get("GITHUB_EVENT_PATH"):
        pr = load_pull_request(Path(env["GITHUB_EVENT_PATH"]))
    base = opts.base or (pr.base_sha if pr else None)
    head = opts.head or (pr.head_sha if pr else "HEAD")
    if not base:
        raise CiError("no base to compare against: run on a pull_request event or pass --base")

    try:
        root = repo_root(cwd)
        rev_parse(root, head)
    except GitError as e:
        raise CiError(
            f"cannot find {head!r} in the checkout ({e}). Check out with fetch-depth: 0."
        ) from e

    mode = opts.mode
    try:
        # The repo's .watchman.toml is read from the base revision, never from the PR.
        cfg, cfg_warnings = load_review_config(root, base)
    except ConfigError as e:
        raise CiError(str(e)) from e
    notes += cfg_warnings
    if opts.provider is not None:
        cfg.llm.provider = opts.provider
    if mode is not ReviewMode.DETERMINISTIC:
        try:
            resolve_api_key(cfg.llm)
        except ConfigError as e:
            why = (
                "this looks like a fork PR, so secrets aren't available"
                if (pr and pr.is_fork)
                else str(e)
            )
            notes.append(f"LLM review skipped, ran Layer 1 only: {why}")
            mode = ReviewMode.DETERMINISTIC

    review_opts = base_options or ReviewOptions()
    review_opts.base, review_opts.head, review_opts.mode = base, head, mode
    review_opts.model = opts.model
    review_opts.checks = opts.checks
    review_opts.task = pr.task() if pr else None
    if opts.deep and mode is not ReviewMode.DETERMINISTIC:
        review_opts.overseer = OverseerTrigger.ALWAYS
    try:
        result = run_review(root, review_opts, cfg)
    except ReviewError as e:
        raise CiError(f"review failed: {e}") from e
    result.warnings = [*notes, *result.warnings]

    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    blob_base = f"{server}/{repo_slug}/blob/{head}" if repo_slug and len(head) == 40 else None
    markdown = render_markdown(result, blob_base=blob_base)

    out_dir = opts.out_dir or Path(env.get("RUNNER_TEMP") or tempfile.mkdtemp(prefix="watchman-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / "watchman-review.json", out_dir / "watchman-review.md"
    json_path.write_text(result.model_dump_json(indent=2) + "\n")
    md_path.write_text(markdown)
    append_summary(env.get("GITHUB_STEP_SUMMARY"), markdown)

    action = url = None
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if opts.comment and pr is not None and repo_slug and token:
        client = http or httpx.Client(timeout=30.0)
        api = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        try:
            action, url = upsert_comment(client, api, repo_slug, pr.number, markdown, token)
        except httpx.HTTPError as e:
            notes.append(
                f"could not post the PR comment ({type(e).__name__}); the result is in the job "
                "summary. Fork PRs have a read-only token, and the job needs "
                "`pull-requests: write`."
            )
    elif opts.comment and pr is not None and not token:
        notes.append("no GITHUB_TOKEN available; skipped the PR comment (see the job summary)")

    exit_code = 0
    if gate(result.verdict, opts.fail_on) or (opts.strict and result.incomplete):
        exit_code = 1
    write_outputs(
        env.get("GITHUB_OUTPUT"),
        {
            "verdict": result.verdict.value,
            "findings": str(len(result.findings)),
            "incomplete": str(result.incomplete).lower(),
            "cost-usd": f"{result.usage.cost_usd:.4f}",
            "json-path": str(json_path),
            "markdown-path": str(md_path),
            "comment-url": url or "",
        },
    )
    return CiOutcome(result, markdown, json_path, md_path, action, url, exit_code, notes)
