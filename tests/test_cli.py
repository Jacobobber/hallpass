"""The CLI is a thin shell over the library. These check the pieces that don't
block on a socket: catalog list/search print real data, doctor runs and returns
a sensible exit code, and the parser wires the subcommands."""

import pytest

from hallpass.cli import build_parser, main


def test_catalog_list(capsys):
    rc = main(["catalog", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "github" in out
    assert "tools" in out
    # per-tenant and oauth markers show up
    assert "per-tenant" in out and "oauth" in out


def test_catalog_search_ranks_the_right_tool(capsys):
    rc = main(["catalog", "search", "list my github repositories", "--limit", "5"])
    out = capsys.readouterr().out
    assert rc == 0
    # the obvious match should be in the top few
    assert "github_list_my_repos" in out


def test_doctor_dev_runs_and_reports(capsys):
    rc = main(["doctor", "--dev"])
    out = capsys.readouterr().out
    # dev app has connectors (tools ok) but no audit/rate/durable vault (warns)
    assert "tools" in out
    assert "WARN" in out
    assert rc == 0  # warnings are not errors


def test_serve_parser_wires_flags():
    args = build_parser().parse_args(["serve", "--dev", "--port", "9001"])
    assert args.command == "serve" and args.dev is True and args.port == 9001


def test_doctor_without_env_exits_with_message(capsys, monkeypatch):
    for var in ("HALLPASS_ISSUER", "HALLPASS_AUDIENCE", "HALLPASS_JWKS_URL"):
        monkeypatch.delenv(var, raising=False)
    # no --dev and no env -> SystemExit with the missing-var guidance
    with pytest.raises(SystemExit) as exc:
        main(["doctor"])
    assert "HALLPASS_ISSUER" in str(exc.value)
