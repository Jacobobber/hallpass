"""``doctor()``: a read-only self-check of a built Hallpass app.

Answers "is this server actually set up to serve users, or will it embarrass
me at the first request?" -- before that first request, without a network call
or a real tool call. It is the thing a newcomer runs after wiring an app to
find out what they forgot: no connectors, an in-memory vault that drops every
credential on restart, no audit trail, no rate limit.

    from hallpass import doctor, format_report
    print(format_report(doctor(app)))

Findings are advisory, not fatal: only ``no-tools`` is an error (a server with
nothing to serve). The rest are warnings a single-process demo can ignore but a
real deployment should not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .core import Hallpass

__all__ = ["Finding", "doctor", "format_report"]

Level = Literal["ok", "warn", "error"]


@dataclass(frozen=True)
class Finding:
    level: Level
    code: str
    message: str


def doctor(app: Hallpass) -> list[Finding]:
    """Inspect a built app and return findings, most severe first. Pure
    introspection: nothing here calls the network or runs a tool."""
    findings: list[Finding] = []

    tools = app.tool_names
    if not tools:
        findings.append(
            Finding(
                "error",
                "no-tools",
                "No tools are registered. Add a connector, e.g. "
                "build(connectors=[catalog.load('github')]) or app.add_connector(...).",
            )
        )
    else:
        findings.append(
            Finding(
                "ok",
                "tools",
                f"{len(tools)} tool(s) across {len(app.connector_names)} connector(s).",
            )
        )

    if app.unavailable_connectors:
        skipped = ", ".join(sorted(app.unavailable_connectors))
        findings.append(
            Finding(
                "warn",
                "unavailable-connectors",
                f"Skipped as unavailable at registration: {skipped}. "
                "Their backends looked unconfigured, so their tools are not served.",
            )
        )

    if not app.has_audit:
        findings.append(
            Finding(
                "warn",
                "no-audit",
                "No audit sink: tool calls and denials are not recorded. "
                "Pass audit= for anything multi-user.",
            )
        )

    if not app.has_rate_limiter:
        findings.append(
            Finding(
                "warn",
                "no-rate-limit",
                "No rate limiter: one caller can exhaust a downstream quota for "
                "everyone. Pass rate_limit=(max_calls, window_seconds).",
            )
        )

    if not app.vault_durable:
        findings.append(
            Finding(
                "warn",
                "ephemeral-vault",
                "Credential vault is in-memory: every connected credential is "
                "lost on restart. Pass vault_path= (and a fixed vault_key=) to persist.",
            )
        )

    # Most severe first so a reader sees the blocker at the top.
    order = {"error": 0, "warn": 1, "ok": 2}
    return sorted(findings, key=lambda f: order[f.level])


def format_report(findings: list[Finding]) -> str:
    """Render findings as aligned lines for a terminal or a log."""
    tag = {"ok": "OK  ", "warn": "WARN", "error": "ERR "}
    return "\n".join(f"[{tag[f.level]}] {f.code}: {f.message}" for f in findings)
