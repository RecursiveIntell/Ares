"""Synthetic headless/manual launch controls; no actual browser/server launch."""
import argparse
from urllib.parse import urlsplit, parse_qs
from hermes_cli import web_server as ws
from hermes_cli.subcommands.dashboard import build_dashboard_parser


def test_print_launch_url_is_explicit_and_supported_by_dashboard_parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    build_dashboard_parser(sub, cmd_dashboard=lambda args: None, cmd_dashboard_register=lambda args: None)
    args = parser.parse_args(["dashboard", "--no-open", "--print-launch-url", "--isolated"])
    assert args.print_launch_url and args.no_open and args.isolated


def test_headless_print_issues_fragment_grant_not_bearer(monkeypatch, capsys):
    import webbrowser
    monkeypatch.setattr(ws.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(ws.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(webbrowser, "open", lambda url: (_ for _ in ()).throw(AssertionError("must not open a browser")))
    ws._maybe_open_browser("::1", 9119, False, "named profile", True)
    url = capsys.readouterr().out.strip()
    parsed = urlsplit(url)
    assert parsed.hostname == "::1" and parsed.port == 9119
    assert parse_qs(parsed.query) == {"profile": ["named profile"]}
    ticket = parse_qs(parsed.fragment)["bootstrap"][0]
    assert ticket != ws._SESSION_TOKEN
    assert ws._SESSION_TOKEN not in url
    assert ws._BROWSER_LAUNCH_TICKETS.consume(ticket)
    assert not ws._BROWSER_LAUNCH_TICKETS.consume(ticket)
