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


def test_migrate_parser_wired():
    args = build_parser().parse_args(["migrate"])
    assert args.command == "migrate"


def test_migrate_without_database_url_exits(monkeypatch):
    monkeypatch.delenv("HALLPASS_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["migrate"])
    assert "HALLPASS_DATABASE_URL" in str(exc.value)


def test_doctor_without_env_exits_with_message(capsys, monkeypatch):
    for var in ("HALLPASS_ISSUER", "HALLPASS_AUDIENCE", "HALLPASS_JWKS_URL"):
        monkeypatch.delenv(var, raising=False)
    # no --dev and no env -> SystemExit with the missing-var guidance
    with pytest.raises(SystemExit) as exc:
        main(["doctor"])
    assert "HALLPASS_ISSUER" in str(exc.value)


def test_serve_dev_refused_with_production_signal(monkeypatch):
    """serve --dev wires a token forger; it must refuse when a production signal
    is present (a shared database), so it can never run reachable in prod."""
    monkeypatch.setenv("HALLPASS_DATABASE_URL", "postgresql://db/x")
    with pytest.raises(SystemExit) as exc:
        main(["serve", "--dev"])
    assert "dev" in str(exc.value).lower()


def test_app_from_env_wires_audit_from_path(monkeypatch, tmp_path):
    """A production serve wired no audit before; now HALLPASS_AUDIT_PATH gives a
    durable SQLite trail (and HALLPASS_DATABASE_URL would give shared Postgres)."""
    from hallpass.cli import _app_from_env

    monkeypatch.setenv("HALLPASS_ISSUER", "https://issuer.example")
    monkeypatch.setenv("HALLPASS_AUDIENCE", "https://api.example")
    monkeypatch.setenv("HALLPASS_JWKS_URL", "https://issuer.example/jwks")
    monkeypatch.delenv("HALLPASS_DATABASE_URL", raising=False)
    monkeypatch.setenv("HALLPASS_AUDIT_PATH", str(tmp_path / "audit.db"))
    app = _app_from_env()
    assert app.has_audit is True
    app.close()


def test_app_from_env_no_audit_without_config(monkeypatch):
    from hallpass.cli import _app_from_env

    monkeypatch.setenv("HALLPASS_ISSUER", "https://issuer.example")
    monkeypatch.setenv("HALLPASS_AUDIENCE", "https://api.example")
    monkeypatch.setenv("HALLPASS_JWKS_URL", "https://issuer.example/jwks")
    for var in ("HALLPASS_DATABASE_URL", "HALLPASS_AUDIT_PATH"):
        monkeypatch.delenv(var, raising=False)
    app = _app_from_env()
    assert app.has_audit is False
    app.close()


def test_service_claim_without_values_is_rejected(monkeypatch):
    """A service_claim with no values means no token is ever a service principal,
    silently disabling the human-gate's service refusal. Fail fast."""
    monkeypatch.setenv("HALLPASS_ISSUER", "https://issuer.example")
    monkeypatch.setenv("HALLPASS_AUDIENCE", "https://api.example")
    monkeypatch.setenv("HALLPASS_JWKS_URL", "https://issuer.example/jwks")
    monkeypatch.setenv("HALLPASS_SERVICE_CLAIM", "gty")
    monkeypatch.delenv("HALLPASS_SERVICE_VALUES", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["doctor"])
    assert "HALLPASS_SERVICE_VALUES" in str(exc.value)
