# Multi-user is the hard part of an MCP server

*Everyone ships the tools. Almost nobody ships whose tools they are.*

Write an MCP server and the tutorial path is short: define some tools, wire them to a service, drop the API key in an environment variable, run it over stdio. It works, it demos well, and it is single-user to the bone. One process, one identity, one set of credentials that belongs to whoever started it.

The moment that server fronts real services for more than one person, three questions arrive at once, and the tutorial answered none of them.

## The three questions

**Who is this request from?** A bearer token showed up. Is it valid, did your identity provider issue it, was it issued for *this* server, and which user does it name? Getting any of these wrong has a name. A server that accepts a token minted for a different audience is a confused deputy: a valid user of some other app can spend their token here and act as themselves against tools you never meant to expose to them. The token looks perfect. The signature checks out. It was simply never meant for you.

**Where do this user's credentials live?** Your tools call downstream services (a CRM, a repo host, a ticketing system) and each user has their own account there. So the server holds other people's credentials, which is the most dangerous thing it will ever do. Those secrets cannot sit in plaintext, cannot be keyed so loosely that one user's request can read another's, and cannot leak through the incidental channels: a log line, an exception message, a debug repr.

**Which tools does this user get?** The token carries scopes: what the identity provider says this user consented to. A read-only user should not see the delete tool, and, the part that is easy to get wrong, should not be able to *call* it either. Hiding a tool from the catalog is presentation. Refusing to run it when called anyway is the actual control. A client that never lists and calls straight to `delete_everything` has to hit a wall, or the menu was theater.

## Why the obvious fixes are traps

Each question has a plausible shortcut that fails.

For identity, the shortcut is to verify the signature and stop. But a signature only proves the token is authentic, not that it was meant for you. Skip the audience check and every sibling service in your identity tenant becomes an entry point. Skip the algorithm allowlist and an attacker downgrades an RS256 verifier into accepting an HS256 token signed with your public key, or an `alg=none` token signed with nothing. The verifier has to be strict in ways that feel paranoid until the one time they are not.

For credentials, the shortcut is a shared secrets table the tools read from directly. That works until a query forgets its `WHERE user = ?`, and now the failure mode is not a bug, it is a cross-tenant breach. The isolation cannot depend on every tool author remembering to scope every read. It has to live in the seam the tools are handed, so that reaching another user's data is not forbidden, it is unrepresentable.

For gating, the shortcut is to filter the tool list and trust clients to respect it. But the client is not the boundary. The wire is not the boundary. The only boundary is the server refusing the call, every call, after re-deriving what this specific token is allowed to do.

## The shape that holds

hallpass is one arrangement of these answers, small enough to read in a sitting. Three layers, one premise.

The premise: the token is the only thing you trust, and only after you have verified it yourself. Everything downstream keys off the subject and scopes it proves, never off what a client claims.

Identity is an OAuth 2.1 resource-server verifier, deliberately boring. RS256 against the provider's published keys, exact issuer and audience, an algorithm allowlist that refuses `none` and the symmetric family, one key refresh on an unknown key id and then a closed door. The key source is injected, so the same verification logic runs against a live provider in production and a static document in tests, which is why the test suite needs no network and still exercises the real path.

The vault encrypts each credential at rest and keys it by (user, service). Tools never touch the vault directly. They receive a context that can reach exactly one thing: the calling user's credential for the calling connector's service. A tool cannot read across users because it is never handed the means to.

Gating derives the catalog per user, and re-checks at call time. The two checks are not redundant. The catalog is a convenience for well-behaved clients; the call-time check is the security boundary, and it does not care whether the client looked at the menu.

## Tests are the argument

Claims about security age badly in prose and well in a suite. Every property above is a test that names the attack it defeats: the confused-deputy wrong-audience token, the `alg=none` forgery, the HS256 downgrade, a signature from the wrong key, secrets found in the raw database file, one user's context reaching another's credential, a direct call to an ungranted tool. A design change that reopens any of them fails with the name of the hole it reopened.

That is the part worth copying, more than any single line of this code. When the thing you are building is a boundary, write the ways through it down as tests first. The suite is the specification, and it is the only version of the specification that cannot quietly drift from what the code actually does.

The reference implementation is at [github.com/Jacobobber/hallpass](https://github.com/Jacobobber/hallpass).
