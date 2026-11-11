# chimerakit

The Python client for the Chimera partner bank API. Your bank keeps its own bot and its own clients. Chimera holds the funds and the ledger.

    pip install chimerakit

Requires Python 3.9 or newer. MIT licensed.

## Try it first, with no real funds

The sandbox is a pretend Chimera on your own computer. It holds no real funds and needs no account:

    python examples/quickstart.py

To point your own bot at it, run `python -m chimerakit.sandbox`, then use this:

```python
from chimerakit import Chimera
from chimerakit.sandbox import SANDBOX_SECRET

chimera = Chimera(SANDBOX_SECRET, base_url="http://127.0.0.1:8099")
```

The sandbox gives the same answers as the real API, and a test in Chimera's repository holds the two together. It has no `under_review` queue with staff behind it, and it never expires a code.

## Using it

`Chimera` is for a blocking program. `AsyncChimera` has the same methods for a Discord bot or anything on asyncio. See `examples/discord_bot.py`.

| Method | What it does |
|---|---|
| `open_account(discord_id, nation_id)` | Opens an empty account. Harmless to repeat |
| `accounts(after, limit)`, `iter_accounts()` | Your clients' balances, each with any deposits on the way |
| `create_deposit(discord_id, nation_id, amounts, request_id)` | Says funds are coming, and gets the code |
| `get_deposit(deposit_id)` | `waiting`, `credited`, `expired` or `under_review` |
| `create_handoff(lines, request_id)` | One transfer for up to 500 clients |
| `get_handoff(handoff_id)` | The same four statuses |
| `create_withdrawal(discord_id, nation_id, amounts, request_id, alliance_id)` | Takes the amounts off the balance at once. `alliance_id` is optional |
| `get_withdrawal(withdrawal_id)` | `processing`, `sent`, `completed`, `failed` or `under_review` |
| `confirm_withdrawal(withdrawal_id, code)` | Says the funds reached your alliance |

### A deposit

1. `create_deposit` gives you a `code`, an `alliance_id` and `amounts`.
2. Send exactly those amounts, in the game, from your registered alliance to Chimera (alliance 15065).
3. The code must be the **whole note**, with nothing around it. A note like `deposit ABCD...` is not credited.
4. Do it before `expires_at`. Funds that arrive late are not lost, but a person has to settle them.
5. Watch `get_deposit` until it says `credited`.

Only a transfer from your registered alliance counts. Sends from a nation, by trade or from another alliance are never credited automatically.

A client can have one deposit in progress at a time.

### A handoff

The tool for a raid: everything leaves your alliance in one transfer, and each client is credited their share.

```python
from chimerakit import Line

handoff = chimera.create_handoff([
    Line(discord_id="1100000000000000001", nation_id=111, amounts={"money": "250.00"}),
    Line(discord_id="1100000000000000002", nation_id=222, amounts={"money": "750.00", "steel": "10"}),
])
```

Send `handoff.amounts`, the total, with `handoff.code` as the whole note. It credits every line or none. Every client needs an account first, and a client frozen or unknown refuses the whole handoff. If the amounts differ by a cent, nobody is credited and a person settles it, so send the total exactly. Limits count the total.

A handoff doesn't stop a client depositing, and a client can be in more than one.

### A withdrawal

1. `create_withdrawal` takes the funds off the client's balance at once.
2. Chimera sends them to an alliance, never to a client's nation: your registered alliance, or the one you name with `alliance_id`. The note is the withdrawal's `code`.
3. When the status is `sent`, find the transfer in the receiving alliance's bank record and check that its note equals `code`.
4. Pay your client, then call `confirm_withdrawal`.
5. It becomes `completed` when your confirmation and the game's record agree.

A large withdrawal may wait for a person, and it stays `processing` or shows `under_review` while it does. Keep checking, and don't send a second one.

## Amounts

Give amounts as `Decimal`, `int` or `str`, with at most two decimal places. A `float` is refused, because it has already been rounded. Amounts come back as `Decimal`.

The names are `money`, `food`, `coal`, `oil`, `uranium`, `lead`, `iron`, `bauxite`, `gasoline`, `munitions`, `steel` and `aluminum`.

## Retries

Failed calls are tried again automatically, up to four tries in all, and a retry cannot act twice. Each deposit, handoff and withdrawal carries a request ID, and Chimera acts on an ID once.

If you let the client make the ID, it still protects its own retries. To be safe across a crash, make the ID yourself with `new_request_id()`, save it, and only then make the call.

If the tries run out you get `Unavailable`. **That doesn't mean the call failed.** It means there was no clear answer. Repeat the call with `e.request_id`, and never with a new ID: a new ID on a withdrawal could take the funds twice.

Using an ID again with different details is refused with `Conflict`.

## Errors

Each one has `status` and `request_id`, and they all come from `ChimeraError`.

| Error | Means | What to do |
|---|---|---|
| `AccessDenied` | Wrong or revoked secret, or your bank isn't switched on | Don't retry. Talk to Chimera |
| `LimitExceeded` | Over your bank's limit for one transfer or the day | Don't retry. Limits are set by Chimera |
| `NotFound` | Missing, or not yours | Don't retry |
| `Conflict` | Frozen, not enough funds, a deposit in progress, an ID reused | Read the message |
| `InvalidRequest` | The request itself is wrong | Fix it |
| `RateLimited` | Over 300 calls a minute, after the retries | Slow down |
| `Unavailable` | No clear answer, after the retries | Repeat with the same ID |

## Your secret

Treat it like a key to your clients' funds, because it is one. Keep it in an environment variable. Never commit it or log it. If it leaks, tell Chimera at once: it can be replaced in seconds, and your clients' balances don't change. The client refuses to send it anywhere but HTTPS, apart from this computer for the sandbox, and it doesn't follow redirects.

### Naming the alliance

```python
withdrawal = chimera.create_withdrawal(discord_id, nation_id, {"money": "1000000"}, alliance_id=4242)
```

There is no list to be on and no extra approval, so check the number before you call: funds sent to the wrong alliance cannot
be pulled back by Chimera. Take the number from a source you trust, never straight from a client's message. A retry with the
same request ID must name the same alliance.

## Development

    pip install -e ".[test]"
    pytest

Releases are built and published by GitHub Actions when a `v*` tag is pushed (see `.github/workflows/release.yml`).
