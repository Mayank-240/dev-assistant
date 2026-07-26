"""Offline tests for the GitHub integration core (src/ai_dev_assistant/github.py).

No network: HTTP goes through an injected fake transport and git through a
monkeypatched subprocess.run.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_dev_assistant import github as gh
from ai_dev_assistant.github import GitHubClient, GitHubConfig

TOKEN = "ghp_secret_token_123"

_ENV_KEYS = ("GITHUB_TOKEN", "ADA_GITHUB_TOKEN", "ADA_GITHUB_LABEL", "ADA_GITHUB_REPOS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class FakeTransport:
    """Records calls; replays a scripted list of (status, json) responses."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str, dict, dict | None]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        if not self.responses:
            return 200, []
        return self.responses.pop(0)


def _raising_transport(method, url, headers, body):
    raise ConnectionError("network down")


def _client(responses=None):
    cfg = GitHubConfig(token=TOKEN, label="ada", repo_map={"o/r": "proj"})
    transport = FakeTransport(responses)
    return GitHubClient(cfg, transport=transport), transport


def _issue(number, title="Fix the bug", body="details", label="ada", **extra):
    item = {
        "number": number,
        "title": title,
        "body": body,
        "html_url": f"https://github.com/o/r/issues/{number}",
        "labels": [{"name": label}],
    }
    item.update(extra)
    return item


# ---------------------------------------------------------------------------
# GitHubConfig.from_env / enabled
# ---------------------------------------------------------------------------

def test_from_env_defaults():
    cfg = GitHubConfig.from_env()
    assert cfg.token == ""
    assert cfg.label == "ada"
    assert cfg.repo_map == {}
    assert cfg.enabled is False


def test_from_env_reads_github_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok-a")
    assert GitHubConfig.from_env().token == "tok-a"


def test_from_env_prefers_ada_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok-a")
    monkeypatch.setenv("ADA_GITHUB_TOKEN", "tok-b")
    assert GitHubConfig.from_env().token == "tok-b"


def test_from_env_label_and_repo_map(monkeypatch):
    monkeypatch.setenv("ADA_GITHUB_LABEL", "assistant")
    monkeypatch.setenv("ADA_GITHUB_REPOS",
                       "owner/repo=project-slug, owner/other=slug2")
    cfg = GitHubConfig.from_env()
    assert cfg.label == "assistant"
    assert cfg.repo_map == {"owner/repo": "project-slug", "owner/other": "slug2"}


def test_from_env_repo_map_skips_malformed(monkeypatch):
    monkeypatch.setenv(
        "ADA_GITHUB_REPOS",
        "owner/repo=good,noequals,norepo=slug,owner/empty=, ,owner/x=ok",
    )
    cfg = GitHubConfig.from_env()
    assert cfg.repo_map == {"owner/repo": "good", "owner/x": "ok"}


def test_from_env_blank_label_falls_back(monkeypatch):
    monkeypatch.setenv("ADA_GITHUB_LABEL", "   ")
    assert GitHubConfig.from_env().label == "ada"


@pytest.mark.parametrize("token,repos,expected", [
    ("t", {"o/r": "p"}, True),
    ("t", {}, False),
    ("", {"o/r": "p"}, False),
    ("", {}, False),
])
def test_enabled_requires_token_and_repos(token, repos, expected):
    assert GitHubConfig(token=token, repo_map=repos).enabled is expected


# ---------------------------------------------------------------------------
# list_labeled_issues
# ---------------------------------------------------------------------------

def test_list_issues_filters_and_shapes():
    items = [
        _issue(1),
        _issue(2, pull_request={"url": "..."}),        # a PR — must be dropped
        _issue(3, label="other"),                      # wrong label — dropped
    ]
    client, transport = _client([(200, items)])
    out = client.list_labeled_issues("o/r")
    assert out == [{
        "repo": "o/r", "number": 1, "title": "Fix the bug",
        "body": "details", "url": "https://github.com/o/r/issues/1",
    }]
    method, url, headers, body = transport.calls[0]
    assert method == "GET" and body is None
    assert url.startswith("https://api.github.com/repos/o/r/issues?")
    assert "state=open" in url and "labels=ada" in url
    assert "per_page=100" in url and "page=1" in url
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Accept"] == "application/vnd.github+json"


def test_list_issues_paginates_on_full_page():
    page1 = [_issue(i) for i in range(1, 101)]
    page2 = [_issue(200)]
    client, transport = _client([(200, page1), (200, page2)])
    out = client.list_labeled_issues("o/r")
    assert len(out) == 101
    assert len(transport.calls) == 2
    assert "page=1" in transport.calls[0][1]
    assert "page=2" in transport.calls[1][1]


def test_list_issues_stops_on_short_page():
    client, transport = _client([(200, [_issue(1), _issue(2)])])
    assert len(client.list_labeled_issues("o/r")) == 2
    assert len(transport.calls) == 1


def test_list_issues_http_error_returns_empty():
    client, _ = _client([(500, {"message": "boom"})])
    assert client.list_labeled_issues("o/r") == []


def test_list_issues_transport_exception_returns_empty():
    cfg = GitHubConfig(token=TOKEN, repo_map={"o/r": "p"})
    client = GitHubClient(cfg, transport=_raising_transport)
    assert client.list_labeled_issues("o/r") == []


def test_list_issues_trusts_server_when_labels_field_missing():
    item = _issue(7)
    del item["labels"]
    client, _ = _client([(200, [item])])
    assert [i["number"] for i in client.list_labeled_issues("o/r")] == [7]


# ---------------------------------------------------------------------------
# comment
# ---------------------------------------------------------------------------

def test_comment_payload_and_success():
    client, transport = _client([(201, {"id": 1})])
    assert client.comment("o/r", 5, "hello") is True
    method, url, headers, body = transport.calls[0]
    assert method == "POST"
    assert url == "https://api.github.com/repos/o/r/issues/5/comments"
    assert body == {"body": "hello"}
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_comment_failure_paths():
    client, _ = _client([(403, {"message": "forbidden"})])
    assert client.comment("o/r", 5, "hello") is False
    cfg = GitHubConfig(token=TOKEN, repo_map={"o/r": "p"})
    raising = GitHubClient(cfg, transport=_raising_transport)
    assert raising.comment("o/r", 5, "hello") is False


# ---------------------------------------------------------------------------
# open_pr
# ---------------------------------------------------------------------------

def test_open_pr_payload_and_result():
    client, transport = _client([
        (201, {"html_url": "https://github.com/o/r/pull/9", "number": 9}),
    ])
    pr = client.open_pr("o/r", head="ada/task-1", base="main",
                        title="feat: x", body="evidence")
    assert pr == {"url": "https://github.com/o/r/pull/9", "number": 9}
    method, url, headers, body = transport.calls[0]
    assert method == "POST"
    assert url == "https://api.github.com/repos/o/r/pulls"
    assert body == {"title": "feat: x", "head": "ada/task-1",
                    "base": "main", "body": "evidence"}
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_open_pr_failure_returns_none():
    client, _ = _client([(422, {"message": "Validation Failed"})])
    assert client.open_pr("o/r", head="h", base="b", title="t", body="") is None


def test_open_pr_transport_exception_returns_none():
    cfg = GitHubConfig(token=TOKEN, repo_map={"o/r": "p"})
    client = GitHubClient(cfg, transport=_raising_transport)
    assert client.open_pr("o/r", head="h", base="b", title="t", body="") is None


# ---------------------------------------------------------------------------
# issue_to_prompt
# ---------------------------------------------------------------------------

def test_issue_to_prompt_strips_html_comments():
    issue = {
        "title": "Add caching",
        "body": "<!-- template: fill this in -->\nCache the results.\n"
                "<!-- multi\nline\ncomment -->\nUse an LRU.",
    }
    prompt = gh.issue_to_prompt(issue)
    assert prompt.startswith("Add caching")
    assert "Cache the results." in prompt and "Use an LRU." in prompt
    assert "<!--" not in prompt and "template" not in prompt


def test_issue_to_prompt_strips_unclosed_comment():
    prompt = gh.issue_to_prompt({"title": "T", "body": "real text\n<!-- dangling"})
    assert "dangling" not in prompt and "real text" in prompt


def test_issue_to_prompt_caps_length():
    prompt = gh.issue_to_prompt({"title": "T", "body": "x" * 50_000})
    assert len(prompt) == 10_000


def test_issue_to_prompt_title_only_and_junk():
    assert gh.issue_to_prompt({"title": "Just a title", "body": None}) == "Just a title"
    assert gh.issue_to_prompt({"title": "", "body": ""}) == ""
    assert gh.issue_to_prompt(None) == ""
    assert gh.issue_to_prompt("not a dict") == ""


# ---------------------------------------------------------------------------
# pr_body
# ---------------------------------------------------------------------------

RUN_SUMMARY = {
    "tldr": "Implemented the caching layer with an LRU and TTL support.",
    "key_points": ["Added cache.py with LRU + TTL.", "All tests pass."],
    "subtasks": [
        {"id": "s1", "title": "Implement cache", "status": "passed", "score": 92},
        {"id": "s2", "title": "Docs | examples", "status": "failed", "score": 40},
        {"id": "s3", "status": "passed"},
    ],
    "branch": "ada/task-42",
    "tests": "passed",
    "quality_score": 0.87,
    "cost_usd": 1.239,
}


def test_pr_body_renders_evidence():
    body = gh.pr_body(RUN_SUMMARY)
    assert "## Summary" in body
    assert RUN_SUMMARY["tldr"] in body
    assert "- Added cache.py with LRU + TTL." in body
    assert "| Subtask | Verdict | Score |" in body
    assert "| Implement cache | passed | 92 |" in body
    assert "Docs \\| examples" in body          # pipes escaped in cells
    assert "| s3 | passed | - |" in body        # id fallback, missing score
    assert "`ada/task-42`" in body
    assert "**Tests:** passed" in body
    assert "**Quality:** 0.87" in body
    assert "**Cost:** $1.24" in body


def test_pr_body_tolerates_empty_and_junk():
    assert "## Summary" in gh.pr_body({})
    assert isinstance(gh.pr_body(None), str)
    assert isinstance(gh.pr_body({"subtasks": "not-a-list", "key_points": None}), str)


# ---------------------------------------------------------------------------
# push_branch
# ---------------------------------------------------------------------------

class _FakeRun:
    def __init__(self, returncode=0, stdout="", stderr="", raise_exc=None):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.raise_exc = raise_exc
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if self.raise_exc:
            raise self.raise_exc
        return SimpleNamespace(returncode=self.returncode,
                               stdout=self.stdout, stderr=self.stderr)


def test_push_branch_success_and_no_token_leak(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    fake = _FakeRun(returncode=0)
    monkeypatch.setattr(gh.subprocess, "run", fake)
    env_before = dict(os.environ)

    out = gh.push_branch(Path("/tmp/repo"), "ada/task-1")

    assert out == {"pushed": True, "remote": "origin", "branch": "ada/task-1"}
    argv, kwargs = fake.calls[0]
    assert argv == ["git", "push", "-u", "origin", "ada/task-1"]
    assert TOKEN not in " ".join(argv)                 # token never in argv
    assert "://" not in " ".join(argv[2:])             # no URL rewriting
    assert "env" not in kwargs                          # inherits user's git auth
    assert kwargs["cwd"] == "/tmp/repo"
    assert dict(os.environ) == env_before               # no env mutation


def test_push_branch_custom_remote(monkeypatch):
    fake = _FakeRun(returncode=0)
    monkeypatch.setattr(gh.subprocess, "run", fake)
    out = gh.push_branch(Path("/tmp/repo"), "b", remote="upstream")
    assert out["pushed"] is True and out["remote"] == "upstream"
    assert fake.calls[0][0] == ["git", "push", "-u", "upstream", "b"]


def test_push_branch_git_failure(monkeypatch):
    fake = _FakeRun(returncode=128, stderr="fatal: could not read from remote")
    monkeypatch.setattr(gh.subprocess, "run", fake)
    out = gh.push_branch(Path("/tmp/repo"), "b")
    assert out["pushed"] is False
    assert "could not read from remote" in out["error"]


def test_push_branch_subprocess_exception(monkeypatch):
    fake = _FakeRun(raise_exc=subprocess.TimeoutExpired(cmd="git", timeout=120))
    monkeypatch.setattr(gh.subprocess, "run", fake)
    out = gh.push_branch(Path("/tmp/repo"), "b")
    assert out["pushed"] is False and out["error"]

    fake = _FakeRun(raise_exc=OSError("git not found"))
    monkeypatch.setattr(gh.subprocess, "run", fake)
    out = gh.push_branch(Path("/tmp/repo"), "b")
    assert out["pushed"] is False and "git not found" in out["error"]


# ---------------------------------------------------------------------------
# seen_marker / never-raises probes
# ---------------------------------------------------------------------------

def test_seen_marker():
    assert gh.seen_marker({"repo": "o/r", "number": 5}) == "o/r#5"
    assert gh.seen_marker({"repo": "o/r"}) == "o/r#"
    assert gh.seen_marker({}) == "#"
    assert gh.seen_marker(None) == ""


def test_client_never_raises_on_hostile_transport():
    def hostile(method, url, headers, body):
        raise RuntimeError("boom")

    client = GitHubClient(GitHubConfig(token="t", repo_map={"o/r": "p"}),
                          transport=hostile)
    assert client.list_labeled_issues("o/r") == []
    assert client.comment("o/r", 1, "x") is False
    assert client.open_pr("o/r", head="h", base="b", title="t", body="") is None


def test_client_never_raises_on_malformed_responses():
    # Non-list issues payload, non-dict PR payload, weird statuses.
    client, _ = _client([(200, {"not": "a list"})])
    assert client.list_labeled_issues("o/r") == []
    client, _ = _client([(201, ["not", "a", "dict"])])
    assert client.open_pr("o/r", head="h", base="b", title="t", body="") is None
    client, _ = _client([(0, None)])
    assert client.comment("o/r", 1, "x") is False
