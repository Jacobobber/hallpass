# Security

hallpass is an auth boundary, so its threat model is the point of the
project, not an afterthought. The properties it enforces, and the attacks
each one refuses, are documented in the module docstrings
([identity.py](src/hallpass/identity.py), [vault.py](src/hallpass/vault.py),
[gating.py](src/hallpass/gating.py)) and proven in the `tests/` failure-mode
suites.

In scope: token verification (audience, issuer, algorithm, signature, key
rotation), credential isolation at rest and across users, and scope gating
at call time.

Out of scope by design: hallpass is not an identity provider (bring your
own OIDC issuer) and does not manage the encryption key's lifecycle (the
operator supplies and rotates it).

Report a vulnerability through GitHub private vulnerability reporting on
this repository (Security tab, "Report a vulnerability"). Reports within
the stated scope get a response within a week.
