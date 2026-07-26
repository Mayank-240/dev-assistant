"""GitHub integration core (poll-based, no inbound webhooks).

Pure, offline-testable building blocks for the GitHub flow:

- find open issues labeled for the assistant on configured repos and map them
  to projects (``GitHubConfig.repo_map``: "owner/repo" -> project slug);
- turn an issue into a clean task prompt (``issue_to_prompt``);
- after a run completes, push the task branch (``push_branch``) and open a PR
  whose body carries the brief + verification evidence (``pr_body``,
  ``GitHubClient.open_pr``).

The server wiring (poll loop, run triggering, dedupe store) lives elsewhere;
``seen_marker`` provides the dedupe key it persists.

Design contract:
- REST v3 (api.github.com), ``Authorization: Bearer``, 10s timeouts.
- The HTTP transport is injectable (``transport(method, url, headers, body) ->
  (status, json)``) so everything is testable without a network.
- No public function or method in this module ever raises: failures are logged
  and surfaced as graceful return values ([], False, None, {"pushed": False}).
- The token is only ever sent as a request header. It is NEVER embedded in
  remote URLs, git argv, or environment mutations — ``push_branch`` relies on
  the user's existing git auth (the remotes of in-place local projects are the
  user's own).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("ada.github")

API_ROOT = "https://api.github.com"
_TIMEOUT = 10.0            # seconds, per HTTP request
_PER_PAGE = 100            # GitHub's maximum page size
_MAX_PAGES = 10            # safety cap on issue pagination
_MAX_PROMPT = 10_000       # chars, cap for issue_to_prompt

# transport(method, url, headers, body) -> (status_code, parsed_json_or_None)
Transport = Callable[[str, str, dict[str, str], dict | None], tuple[int, Any]]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _parse_repo_map(raw: str) -> dict[str, str]:
    """Parse ``"owner/repo=slug,owner/other=slug2"`` into a dict.

    Malformed entries (missing "=", repo without "/", empty slug) are skipped
    with a warning rather than failing the whole config.
    """
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        repo, sep, slug = part.partition("=")
        repo, slug = repo.strip(), slug.strip()
        if not sep or not slug or "/" not in repo:
            logger.warning("ADA_GITHUB_REPOS: skipping malformed entry %r", part)
            continue
        out[repo] = slug
    return out


@dataclass(frozen=True)
class GitHubConfig:
    """GitHub integration settings, normally loaded from the environment."""

    token: str = ""
    label: str = "ada"
    repo_map: dict[str, str] = field(default_factory=dict)  # "owner/repo" -> project slug

    @classmethod
    def from_env(cls) -> "GitHubConfig":
        """Build from env: ADA_GITHUB_TOKEN (preferred) or GITHUB_TOKEN;
        ADA_GITHUB_LABEL (default "ada"); ADA_GITHUB_REPOS repo->slug map."""
        token = (os.getenv("ADA_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
        label = (os.getenv("ADA_GITHUB_LABEL") or "").strip() or "ada"
        repo_map = _parse_repo_map(os.getenv("ADA_GITHUB_REPOS") or "")
        return cls(token=token, label=label, repo_map=repo_map)

    @property
    def enabled(self) -> bool:
        """The integration runs only with both a token and at least one repo."""
        return bool(self.token) and bool(self.repo_map)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

def _default_transport(method: str, url: str, headers: dict[str, str],
                       body: dict | None) -> tuple[int, Any]:
    """urllib-based transport: returns (status, parsed JSON or None)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 - https only
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:  # 4xx/5xx: still a response
        raw = exc.read()
        status = exc.code
    try:
        return status, json.loads(raw) if raw else None
    except (ValueError, UnicodeDecodeError):
        return status, None


class GitHubClient:
    """Minimal REST v3 client. Every method is safe to call blind: errors are
    logged and returned as [], False, or None — never raised."""

    def __init__(self, cfg: GitHubConfig, transport: Transport | None = None) -> None:
        self.cfg = cfg
        self._transport: Transport = transport or _default_transport

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ai-dev-assistant",
        }
        if self.cfg.token:
            headers["Authorization"] = f"Bearer {self.cfg.token}"
        return headers

    def _request(self, method: str, url: str, body: dict | None = None) -> tuple[int, Any]:
        try:
            return self._transport(method, url, self._headers(), body)
        except Exception as exc:  # transport failures must never propagate
            logger.warning("github request failed: %s %s: %s", method, url, str(exc)[:200])
            return 0, None

    # -- issues -------------------------------------------------------------

    def list_labeled_issues(self, repo: str) -> list[dict]:
        """Open issues on ``repo`` carrying the configured label.

        Returns ``[{"repo", "number", "title", "body", "url"}, ...]`` (the
        ``repo`` key feeds :func:`seen_marker`). Pull requests (which GitHub's
        issues API also returns) are filtered out, and the label is re-checked
        client-side. [] on any error.
        """
        results: list[dict] = []
        label = urllib.parse.quote(self.cfg.label)
        for page in range(1, _MAX_PAGES + 1):
            url = (f"{API_ROOT}/repos/{repo}/issues"
                   f"?state=open&labels={label}&per_page={_PER_PAGE}&page={page}")
            status, data = self._request("GET", url)
            if status != 200 or not isinstance(data, list):
                if page == 1:
                    logger.warning("list_labeled_issues(%s) failed: HTTP %s", repo, status)
                break
            for item in data:
                if not isinstance(item, dict) or "pull_request" in item:
                    continue  # PRs surface in the issues API; we only want issues
                if not self._has_label(item):
                    continue
                results.append({
                    "repo": repo,
                    "number": item.get("number"),
                    "title": str(item.get("title") or ""),
                    "body": str(item.get("body") or ""),
                    "url": str(item.get("html_url") or ""),
                })
            if len(data) < _PER_PAGE:
                break
        return results

    def _has_label(self, item: dict) -> bool:
        labels = item.get("labels")
        if not isinstance(labels, list):
            return True  # server already filtered; trust it when labels are absent
        names = {l.get("name") for l in labels if isinstance(l, dict)}
        return self.cfg.label in names

    def comment(self, repo: str, number: int, body: str) -> bool:
        """Post an issue comment. True on success."""
        url = f"{API_ROOT}/repos/{repo}/issues/{number}/comments"
        status, _ = self._request("POST", url, {"body": body})
        ok = status in (200, 201)
        if not ok:
            logger.warning("comment on %s#%s failed: HTTP %s", repo, number, status)
        return ok

    # -- pull requests ------------------------------------------------------

    def open_pr(self, repo: str, *, head: str, base: str, title: str, body: str) -> dict | None:
        """Open a PR ``head`` -> ``base``. Returns {"url", "number"} or None."""
        url = f"{API_ROOT}/repos/{repo}/pulls"
        payload = {"title": title, "head": head, "base": base, "body": body}
        status, data = self._request("POST", url, payload)
        if status in (200, 201) and isinstance(data, dict):
            return {"url": str(data.get("html_url") or ""), "number": data.get("number")}
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("message") or "")[:200]
        logger.warning("open_pr(%s, %s -> %s) failed: HTTP %s %s", repo, head, base, status, detail)
        return None


# ---------------------------------------------------------------------------
# Flow helpers (pure functions)
# ---------------------------------------------------------------------------

def issue_to_prompt(issue: dict) -> str:
    """Title + body as a clean task prompt.

    Strips HTML comments (issue-template chatter), collapses excess blank
    lines, and caps the result at 10k chars. Never raises; "" for junk input.
    """
    try:
        if not isinstance(issue, dict):
            return ""
        title = str(issue.get("title") or "").strip()
        body = str(issue.get("body") or "")
        body = re.sub(r"<!--.*?(-->|\Z)", "", body, flags=re.DOTALL)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        prompt = f"{title}\n\n{body}" if title and body else (title or body)
        return prompt[:_MAX_PROMPT]
    except Exception:  # defensive: this feeds an automated loop
        return ""


def _md_cell(text: Any) -> str:
    """One-line, pipe-safe markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def pr_body(run_summary: dict) -> str:
    """Evidence-first PR body markdown from a completed run's summary.

    Expected (all optional) keys: tldr, key_points [str], subtasks
    [{id,title,status,score}], branch, tests, quality_score, cost_usd.
    Never raises; tolerates a partial or empty summary.
    """
    try:
        rs = run_summary if isinstance(run_summary, dict) else {}
        lines: list[str] = []

        tldr = str(rs.get("tldr") or rs.get("summary") or "").strip()
        lines += ["## Summary", "", tldr or "_No summary available._", ""]

        points = [str(p).strip() for p in (rs.get("key_points") or []) if str(p).strip()]
        if points:
            lines += ["### Key points", ""]
            lines += [f"- {p}" for p in points]
            lines.append("")

        subtasks = [s for s in (rs.get("subtasks") or []) if isinstance(s, dict)]
        if subtasks:
            lines += ["### Verification", "",
                      "| Subtask | Verdict | Score |",
                      "| --- | --- | --- |"]
            for s in subtasks:
                name = s.get("title") or s.get("id") or "?"
                status = s.get("status") or "unknown"
                score = s.get("score")
                lines.append(f"| {_md_cell(name)} | {_md_cell(status)} "
                             f"| {_md_cell(score if score is not None else '-')} |")
            lines.append("")

        facts: list[str] = []
        branch = str(rs.get("branch") or "").strip()
        if branch:
            facts.append(f"**Branch:** `{branch}`")
        tests = str(rs.get("tests") or "").strip()
        if tests:
            facts.append(f"**Tests:** {tests}")
        quality = rs.get("quality_score")
        if isinstance(quality, (int, float)):
            facts.append(f"**Quality:** {quality:g}")
        cost = rs.get("cost_usd")
        if isinstance(cost, (int, float)):
            facts.append(f"**Cost:** ${cost:.2f}")
        if facts:
            lines += ["---", "", " · ".join(facts), ""]

        return "\n".join(lines).strip() + "\n"
    except Exception as exc:  # defensive: a PR must still open with a stub body
        logger.warning("pr_body rendering failed: %s", exc)
        return "## Summary\n\n_Run summary unavailable._\n"


def push_branch(repo_root: Path, branch: str, remote: str = "origin") -> dict:
    """``git push -u <remote> <branch>`` in ``repo_root`` via subprocess.

    The token is NEVER written into remote URLs, argv, or the environment —
    this relies entirely on the user's existing git auth (credential helper,
    SSH keys). Returns {"pushed": True, "remote", "branch"} on success, or
    {"pushed": False, "error": ...}. Never raises.
    """
    try:
        res = subprocess.run(
            ["git", "push", "-u", remote, branch],
            cwd=str(repo_root), capture_output=True, text=True, timeout=120.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("push_branch(%s, %s) failed: %s", repo_root, branch, exc)
        return {"pushed": False, "error": str(exc)[:300]}
    except Exception as exc:  # belt-and-braces: never raise
        logger.warning("push_branch(%s, %s) failed: %s", repo_root, branch, exc)
        return {"pushed": False, "error": str(exc)[:300]}
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()[:300]
        logger.warning("git push failed for %s: %s", branch, err)
        return {"pushed": False, "error": err or f"git push exited {res.returncode}"}
    return {"pushed": True, "remote": remote, "branch": branch}


def seen_marker(issue: dict) -> str:
    """Stable dedupe key for a polled issue: ``"owner/repo#number"``."""
    try:
        if not isinstance(issue, dict):
            return ""
        repo = str(issue.get("repo") or "").strip()
        number = issue.get("number")
        return f"{repo}#{number if number is not None else ''}"
    except Exception:
        return ""
