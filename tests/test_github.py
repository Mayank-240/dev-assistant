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
# post_issue_comment
# ---------------------------------------------------------------------------

def test_post_issue_comment_payload_and_success():
    client, transport = _client([(201, {"id": 1})])
    assert client.post_issue_comment("o/r", 12, "result") is True
    method, url, headers, body = transport.calls[0]
    assert method == "POST"
    # the issues comment endpoint serves PRs too
    assert url == "https://api.github.com/repos/o/r/issues/12/comments"
    assert body == {"body": "result"}
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_post_issue_comment_no_token_sends_nothing():
    cfg = GitHubConfig(token="", label="ada", repo_map={"o/r": "proj"})
    transport = FakeTransport([(201, {"id": 1})])
    client = GitHubClient(cfg, transport=transport)
    assert client.post_issue_comment("o/r", 12, "result") is False
    assert transport.calls == []  # no anonymous POST


def test_post_issue_comment_failure_paths():
    client, _ = _client([(403, {"message": "forbidden"})])
    assert client.post_issue_comment("o/r", 12, "x") is False
    cfg = GitHubConfig(token=TOKEN, repo_map={"o/r": "p"})
    raising = GitHubClient(cfg, transport=_raising_transport)
    assert raising.post_issue_comment("o/r", 12, "x") is False


# ---------------------------------------------------------------------------
# format_followup_result
# ---------------------------------------------------------------------------

RESULT_VERDICTS = [
    {"id": "s1", "title": "Implement cache", "status": "passed", "score": 92},
    {"id": "s2", "title": "Docs | examples", "status": "failed", "score": 40},
    {"id": "s3", "status": "passed"},
]


def test_format_followup_result_completed_snapshot():
    task = {"id": "t-9", "status": "completed", "cost_usd": 1.239}
    expected = (
        "Addressed reviewer feedback — branch updated.\n"
        "\n"
        "| Subtask | Verdict | Score |\n"
        "| --- | --- | --- |\n"
        "| Implement cache | passed | 92 |\n"
        "| Docs \\| examples | failed | 40 |\n"
        "| s3 | passed | - |\n"
        "\n"
        "**Cost:** $1.24\n"
        "\n"
        "_The existing branch was updated in place — this PR's diff reflects "
        "the new changes._\n"
    )
    assert gh.format_followup_result(12, task, RESULT_VERDICTS) == expected


def test_format_followup_result_failed_variant_snapshot():
    task = {"id": "t-9", "status": "failed"}
    expected = (
        "The follow-up run for PR #12 (task `t-9`) did not complete cleanly "
        "(status: failed); see the run for details.\n"
    )
    assert gh.format_followup_result(12, task, []) == expected


def test_format_followup_result_partial_is_honest_but_keeps_evidence():
    task = {"id": "t-9", "status": "partial", "cost_usd": 0.5}
    out = gh.format_followup_result(7, task, RESULT_VERDICTS)
    assert "did not complete cleanly (status: partial)" in out
    assert "PR #7" in out
    assert "Addressed reviewer feedback" not in out
    assert "updated in place" not in out          # no success footer
    assert "| Implement cache | passed | 92 |" in out
    assert "**Cost:** $0.50" in out


def test_format_followup_result_tolerates_junk():
    out = gh.format_followup_result(1, None, "not-a-list")
    assert "(status: unknown)" in out             # honest fallback, never raises
    assert isinstance(gh.format_followup_result(None, {}, [{"bad": 1}, "junk"]), str)
    # a completed run with no verdicts/cost still renders header + footer
    out = gh.format_followup_result(3, {"status": "completed"}, [])
    assert out.startswith("Addressed reviewer feedback — branch updated.")
    assert "updated in place" in out


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


# ---------------------------------------------------------------------------
# list_open_prs / is_own_pr
# ---------------------------------------------------------------------------

def _pr_item(number, title="feat: add caching", head="ada/task-42", body="evidence"):
    """Raw API pull-request item (head is a dict, as GitHub returns it)."""
    return {"number": number, "title": title, "head": {"ref": head}, "body": body}


def test_list_open_prs_shapes_and_request():
    items = [_pr_item(12), _pr_item(13, head="feature/manual"), "junk-not-a-dict"]
    client, transport = _client([(200, items)])
    out = client.list_open_prs("o/r")
    assert out == [
        {"repo": "o/r", "number": 12, "title": "feat: add caching",
         "head": "ada/task-42", "body": "evidence"},
        {"repo": "o/r", "number": 13, "title": "feat: add caching",
         "head": "feature/manual", "body": "evidence"},
    ]
    method, url, headers, body = transport.calls[0]
    assert method == "GET" and body is None
    assert url.startswith("https://api.github.com/repos/o/r/pulls?")
    assert "state=open" in url and "per_page=100" in url and "page=1" in url
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_list_open_prs_paginates_on_full_page():
    page1 = [_pr_item(i) for i in range(1, 101)]
    client, transport = _client([(200, page1), (200, [_pr_item(200)])])
    assert len(client.list_open_prs("o/r")) == 101
    assert len(transport.calls) == 2
    assert "page=2" in transport.calls[1][1]


def test_list_open_prs_error_paths():
    client, _ = _client([(500, {"message": "boom"})])
    assert client.list_open_prs("o/r") == []
    client, _ = _client([(200, {"not": "a list"})])
    assert client.list_open_prs("o/r") == []
    cfg = GitHubConfig(token=TOKEN, repo_map={"o/r": "p"})
    assert GitHubClient(cfg, transport=_raising_transport).list_open_prs("o/r") == []


def test_list_open_prs_tolerates_missing_head():
    item = _pr_item(7)
    item["head"] = None
    client, _ = _client([(200, [item])])
    assert client.list_open_prs("o/r")[0]["head"] == ""


def test_is_own_pr():
    assert gh.is_own_pr({"head": "ada/task-1"}) is True          # normalized shape
    assert gh.is_own_pr({"head": {"ref": "ada/x"}}) is True      # raw API shape
    assert gh.is_own_pr({"head": "feature/manual"}) is False
    assert gh.is_own_pr({"head": "adafruit/x"}) is False         # prefix incl. slash
    assert gh.is_own_pr({"head": ""}) is False
    assert gh.is_own_pr({}) is False
    assert gh.is_own_pr(None) is False
    assert gh.is_own_pr("not a dict") is False


# ---------------------------------------------------------------------------
# list_pr_comments
# ---------------------------------------------------------------------------

class RoutedTransport:
    """Routes responses by URL substring (first match wins); records calls."""

    def __init__(self, routes):
        self.routes = list(routes)  # [(url_substring, status, json), ...]
        self.calls: list[tuple[str, str, dict, dict | None]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        for substr, status, data in self.routes:
            if substr in url:
                return status, data
        return 200, []

    def urls(self, substr):
        return [u for _, u, _, _ in self.calls if substr in u]


def _routed_client(routes):
    cfg = GitHubConfig(token=TOKEN, label="ada", repo_map={"o/r": "proj"})
    transport = RoutedTransport(routes)
    return GitHubClient(cfg, transport=transport), transport


ME = {"login": "ada-bot-user"}


def _issue_comment(cid, author="alice", body="please fix", created="2026-07-01T10:00:00Z"):
    return {"id": cid, "user": {"login": author}, "body": body, "created_at": created}


def _review_comment(cid, author="bob", body="rename this", path="src/cache.py",
                    line=40, created="2026-07-01T09:00:00Z", **extra):
    item = {"id": cid, "user": {"login": author}, "body": body, "path": path,
            "line": line, "created_at": created}
    item.update(extra)
    return item


def test_list_pr_comments_merges_normalizes_and_orders():
    client, transport = _routed_client([
        ("/user", 200, ME),
        ("/issues/5/comments", 200, [_issue_comment(2, created="2026-07-01T10:00:00Z")]),
        ("/pulls/5/comments", 200, [_review_comment(1, created="2026-07-01T09:00:00Z")]),
    ])
    out = client.list_pr_comments("o/r", 5)
    # Oldest first: the review comment (09:00) precedes the issue comment (10:00).
    assert out == [
        {"id": 1, "author": "bob", "body": "rename this", "path": "src/cache.py",
         "line": 40, "created_at": "2026-07-01T09:00:00Z"},
        {"id": 2, "author": "alice", "body": "please fix", "path": None,
         "line": None, "created_at": "2026-07-01T10:00:00Z"},
    ]
    # Both endpoints were hit, with auth.
    assert len(transport.urls("/issues/5/comments")) == 1
    assert len(transport.urls("/pulls/5/comments")) == 1
    for _, _, headers, _ in transport.calls:
        assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_list_pr_comments_created_at_tiebreak_by_id():
    same = "2026-07-01T10:00:00Z"
    client, _ = _routed_client([
        ("/user", 200, ME),
        ("/issues/", 200, [_issue_comment(9, created=same), _issue_comment(3, created=same)]),
        ("/pulls/", 200, []),
    ])
    assert [c["id"] for c in client.list_pr_comments("o/r", 5)] == [3, 9]


def test_list_pr_comments_review_line_falls_back_to_original_line():
    item = _review_comment(1, line=None, original_line=17)
    client, _ = _routed_client([("/user", 200, ME), ("/issues/", 200, []),
                                ("/pulls/", 200, [item])])
    assert client.list_pr_comments("o/r", 5)[0]["line"] == 17


def test_list_pr_comments_since_filter_is_strictly_after():
    client, _ = _routed_client([
        ("/user", 200, ME),
        ("/issues/", 200, [
            _issue_comment(1, created="2026-07-01T09:00:00Z"),   # before — dropped
            _issue_comment(2, created="2026-07-01T10:00:00Z"),   # equal — dropped
            _issue_comment(3, created="2026-07-01T11:00:00Z"),   # after — kept
        ]),
        ("/pulls/", 200, []),
    ])
    out = client.list_pr_comments("o/r", 5, since="2026-07-01T10:00:00Z")
    assert [c["id"] for c in out] == [3]


def test_list_pr_comments_filters_self_bots_and_junk():
    client, _ = _routed_client([
        ("/user", 200, ME),
        ("/issues/", 200, [
            _issue_comment(1, author="ada-bot-user", body="ADA started task t1"),  # self
            _issue_comment(2, author="github-actions[bot]", body="CI passed"),     # bot
            _issue_comment(3, author="", body="ghost"),                            # no author
            _issue_comment(4, body="   "),                                         # empty body
            {"id": 5, "user": "not-a-dict", "body": "x", "created_at": "z"},       # bad user
            "junk-not-a-dict",
            _issue_comment(6, author="alice", body="real feedback"),
        ]),
        ("/pulls/", 200, []),
    ])
    assert [c["id"] for c in client.list_pr_comments("o/r", 5)] == [6]


def test_list_pr_comments_caches_own_user_lookup():
    client, transport = _routed_client([
        ("/user", 200, ME),
        ("/issues/", 200, [_issue_comment(1)]),
        ("/pulls/", 200, []),
    ])
    client.list_pr_comments("o/r", 5)
    client.list_pr_comments("o/r", 6)
    assert len(transport.urls("api.github.com/user")) == 1  # fetched once, cached


def test_list_pr_comments_survives_user_endpoint_failure():
    # /user failing degrades the self-filter but must not lose others' comments.
    client, transport = _routed_client([
        ("/user", 401, {"message": "bad credentials"}),
        ("/issues/", 200, [_issue_comment(1, author="alice")]),
        ("/pulls/", 200, []),
    ])
    assert [c["id"] for c in client.list_pr_comments("o/r", 5)] == [1]
    # Failure is not cached: the next call retries /user.
    client.list_pr_comments("o/r", 5)
    assert len(transport.urls("api.github.com/user")) == 2


def test_list_pr_comments_error_paths():
    cfg = GitHubConfig(token=TOKEN, repo_map={"o/r": "p"})
    assert GitHubClient(cfg, transport=_raising_transport).list_pr_comments("o/r", 5) == []
    client, _ = _routed_client([("/user", 200, ME),
                                ("/issues/", 500, {"message": "boom"}),
                                ("/pulls/", 200, {"not": "a list"})])
    assert client.list_pr_comments("o/r", 5) == []


# ---------------------------------------------------------------------------
# comments_to_followup / FollowUp
# ---------------------------------------------------------------------------

FOLLOWUP_PR = {"repo": "o/r", "number": 12, "title": "feat: add caching",
               "head": "ada/task-42", "body": "evidence"}

FOLLOWUP_COMMENTS = [
    {"id": 1, "author": "bob", "body": "Rename this\nvariable, please.",
     "path": "src/cache.py", "line": 40, "created_at": "2026-07-01T09:00:00Z"},
    {"id": 2, "author": "carol", "body": "Typo in the docstring.",
     "path": "README.md", "line": None, "created_at": "2026-07-01T09:30:00Z"},
    {"id": 3, "author": "alice", "body": "Add a test for eviction.",
     "path": None, "line": None, "created_at": "2026-07-01T10:00:00Z"},
]


def test_comments_to_followup_snapshot():
    expected = (
        'Address reviewer feedback on PR #12 "feat: add caching" (branch `ada/task-42`).\n'
        "\n"
        "Reviewer comments:\n"
        "- bob on src/cache.py:40 — Rename this variable, please.\n"
        "- carol on README.md — Typo in the docstring.\n"
        "- alice — Add a test for eviction.\n"
        "\n"
        "Address this feedback with changes on the existing branch `ada/task-42` "
        "— do not create a new branch or open a new PR."
    )
    assert gh.comments_to_followup(FOLLOWUP_PR, FOLLOWUP_COMMENTS) == expected


def test_comments_to_followup_accepts_raw_head_shape():
    pr = dict(FOLLOWUP_PR, head={"ref": "ada/task-42"})
    out = gh.comments_to_followup(pr, FOLLOWUP_COMMENTS)
    assert "`ada/task-42`" in out


def test_comments_to_followup_caps_length():
    comments = [{"id": i, "author": "a", "body": "x" * 500} for i in range(50)]
    assert len(gh.comments_to_followup(FOLLOWUP_PR, comments)) == 10_000


def test_comments_to_followup_empty_and_junk():
    assert gh.comments_to_followup(FOLLOWUP_PR, []) == ""
    assert gh.comments_to_followup(FOLLOWUP_PR, None) == ""
    assert gh.comments_to_followup(None, FOLLOWUP_COMMENTS) == ""
    assert gh.comments_to_followup("not a dict", FOLLOWUP_COMMENTS) == ""
    # Comments that render no bullet (junk, empty body/author) yield "".
    junk = ["nope", {"id": 1, "author": "", "body": "x"},
            {"id": 2, "author": "a", "body": "   "}]
    assert gh.comments_to_followup(FOLLOWUP_PR, junk) == ""


def test_followup_dataclass_fields():
    fu = gh.FollowUp(repo="o/r", pr_number=12, branch="ada/task-42",
                     task_hint="feat: add caching", prompt="do it")
    assert (fu.repo, fu.pr_number, fu.branch, fu.task_hint, fu.prompt) == \
        ("o/r", 12, "ada/task-42", "feat: add caching", "do it")


# ---------------------------------------------------------------------------
# no-token behavior (client disabled)
# ---------------------------------------------------------------------------

def test_no_token_client_disabled_and_no_auth_header():
    cfg = GitHubConfig(token="", label="ada", repo_map={"o/r": "proj"})
    assert cfg.enabled is False  # server gates the poll loop on this
    transport = RoutedTransport([
        ("/user", 401, {"message": "requires authentication"}),
        ("/pulls?", 200, [_pr_item(1)]),
        ("/issues/", 200, [_issue_comment(1)]),
        ("/pulls/", 200, []),
    ])
    client = GitHubClient(cfg, transport=transport)
    # Methods still degrade gracefully (never raise) and never send auth.
    assert client.list_open_prs("o/r") != []
    assert [c["id"] for c in client.list_pr_comments("o/r", 5)] == [1]
    for _, _, headers, _ in transport.calls:
        assert "Authorization" not in headers
