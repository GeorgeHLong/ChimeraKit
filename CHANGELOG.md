# Changelog

## 0.2.0 (unreleased)

- `create_withdrawal(..., alliance_id=...)`: name the alliance Chimera sends a withdrawal to. Leave it out to use your
  registered alliance, as before. A retry with the same request ID must name the same alliance.
- The sandbox supports `alliance_id` on withdrawals.
- First public release: MIT license, tests that run against the sandbox alone, and releases built and published from GitHub.

## 0.1.0

- The first version, shared privately with partner banks: accounts, deposits, handoffs, withdrawals, balances, retries
  with request IDs, the error types, `AsyncChimera`, and the sandbox.
