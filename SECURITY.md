# Security

hallpass is an auth boundary, so its threat model is the point of the
project, not an afterthought. The properties it enforces, and the attacks
each one refuses, are documented in the module docstrings
([identity.py](src/hallpass/identity.py), [vault.py](src/hallpass/vault.py),
[gating.py](src/hallpass/gating.py)) and proven in the `tests/` failure-mode
suites.

In scope: token verification (audience, issuer, algorithm, signature, key
rotation), credential isolation at rest and across subjects (users and
agents), and scope gating at call time. The coordination layer is in scope
too, since it rides the same identity and scope model: channel
authorization (post/read gated by scope), opaque denial (an undeclared
channel is indistinguishable from an unauthorized one), and the
untrusted-message sanitization boundary (`sanitize` / `frame_untrusted`,
which neutralize control/escape/bidi/zero-width spoofing but do not claim to
detect semantic prompt injection).

Out of scope by design: hallpass is not an identity provider (bring your
own OIDC issuer) and does not manage the encryption key's lifecycle (the
operator supplies and rotates it).

Report a vulnerability through GitHub private vulnerability reporting on
this repository (Security tab, "Report a vulnerability"). Reports within
the stated scope get a response within a week.
