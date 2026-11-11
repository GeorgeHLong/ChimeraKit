"""The client against the sandbox: every call, the retry rules, the errors, and the async client. No network beyond this computer."""
import asyncio
from decimal import Decimal

import pytest

from chimerakit import AccessDenied, AsyncChimera, Chimera, Conflict, Line, LimitExceeded, NotFound, new_request_id
from chimerakit.sandbox import ALLIANCE_ID, SANDBOX_SECRET, Sandbox

ALICE = ("1100000000000000001", 111)
BOB = ("1100000000000000002", 222)


@pytest.fixture
def world():
    with Sandbox() as game, Chimera(SANDBOX_SECRET, base_url=game.url, pause=0) as chimera:
        chimera.open_account(*ALICE)
        chimera.open_account(*BOB)
        yield game, chimera


def fund(game, chimera, who, money="1000.00"):
    d = chimera.create_deposit(who[0], who[1], {"money": money})
    game.arrive(d.code, money=money)
    return d


def balance(chimera, who, resource="money"):
    return next(a for a in chimera.iter_accounts() if str(a.discord_id) == who[0]).balances[resource]


def test_opening_an_account_twice_is_harmless(world):
    game, chimera = world
    first = chimera.open_account(*ALICE)
    assert chimera.open_account(*ALICE).accountid == first.accountid


def test_a_deposit_is_credited_only_when_the_exact_transfer_arrives(world):
    game, chimera = world
    d = chimera.create_deposit(ALICE[0], ALICE[1], {"money": Decimal("1000.00")})
    assert d.alliance_id == ALLIANCE_ID and len(d.code) == 16
    assert chimera.get_deposit(d.deposit_id).status == "waiting"
    game.arrive(d.code, money="1000.00")
    assert chimera.get_deposit(d.deposit_id).status == "credited"
    assert balance(chimera, ALICE) == Decimal("1000.00")


@pytest.mark.parametrize("mistake", [{"note": "deposit ABC"}, {"alliance_id": 999}, {"money": "999.99"}])
def test_a_wrong_note_sender_or_amount_is_held_not_credited(world, mistake):
    game, chimera = world
    d = chimera.create_deposit(ALICE[0], ALICE[1], {"money": "1000.00"})
    sent = dict({"money": "1000.00"}, **mistake)
    game.arrive(d.code, **sent)
    assert chimera.get_deposit(d.deposit_id).status == "under_review"
    assert balance(chimera, ALICE) == 0


def test_a_handoff_credits_every_line(world):
    game, chimera = world
    h = chimera.create_handoff([Line(ALICE[0], ALICE[1], {"money": "250.00"}), Line(BOB[0], BOB[1], {"money": "750.00", "steel": "10"})])
    game.arrive(h.code, money="1000.00", steel="10")
    assert chimera.get_handoff(h.handoff_id).status == "credited"
    assert balance(chimera, ALICE) == Decimal("250.00") and balance(chimera, BOB, "steel") == Decimal("10.00")


def test_a_withdrawal_debits_at_once_and_completes_on_confirmation(world):
    game, chimera = world
    fund(game, chimera, ALICE)
    w = chimera.create_withdrawal(ALICE[0], ALICE[1], {"money": "400.00"})
    assert balance(chimera, ALICE) == Decimal("600.00") and w.alliance_id == ALLIANCE_ID
    game.send(w.withdrawal_id)
    assert chimera.get_withdrawal(w.withdrawal_id).status == "sent"
    chimera.confirm_withdrawal(w.withdrawal_id, w.code)
    assert chimera.get_withdrawal(w.withdrawal_id).status == "completed"


def test_a_withdrawal_can_name_its_alliance_and_a_retry_cannot_change_it(world):
    game, chimera = world
    fund(game, chimera, ALICE)
    rid = new_request_id()
    w = chimera.create_withdrawal(ALICE[0], ALICE[1], {"money": "100.00"}, request_id=rid, alliance_id=4242)
    assert w.alliance_id == 4242
    again = chimera.create_withdrawal(ALICE[0], ALICE[1], {"money": "100.00"}, request_id=rid, alliance_id=4242)
    assert again.repeat and again.withdrawal_id == w.withdrawal_id
    with pytest.raises(Conflict):
        chimera.create_withdrawal(ALICE[0], ALICE[1], {"money": "100.00"}, request_id=rid, alliance_id=4243)
    assert balance(chimera, ALICE) == Decimal("900.00"), "a retry debited twice"


def test_the_same_request_id_acts_once(world):
    game, chimera = world
    rid = new_request_id()
    a = chimera.create_deposit(ALICE[0], ALICE[1], {"money": "5.00"}, request_id=rid)
    b = chimera.create_deposit(ALICE[0], ALICE[1], {"money": "5.00"}, request_id=rid)
    assert a.deposit_id == b.deposit_id and b.repeat
    with pytest.raises(Conflict):
        chimera.create_deposit(ALICE[0], ALICE[1], {"money": "6.00"}, request_id=rid)


def test_refusals_raise_the_documented_errors(world):
    game, chimera = world
    with pytest.raises(Conflict):
        chimera.create_withdrawal(ALICE[0], ALICE[1], {"money": "1.00"})            # nothing to withdraw
    with pytest.raises(NotFound):
        chimera.get_withdrawal(999999)
    with Chimera("chim_wrong", base_url=game.url, pause=0) as stranger, pytest.raises(AccessDenied):
        stranger.get_deposit(1)


def test_limits_are_enforced(world):
    game, chimera = world
    game.transfer_limit = Decimal("10")
    with pytest.raises(LimitExceeded):
        chimera.create_deposit(ALICE[0], ALICE[1], {"money": "11.00"})


@pytest.mark.parametrize("bad", [{"money": 1.5}, {"money": "1.234"}, {"money": "0"}, {"gold": "1"}, {}])
def test_bad_amounts_never_leave_the_computer(world, bad):
    game, chimera = world
    with pytest.raises((TypeError, ValueError)):
        chimera.create_deposit(ALICE[0], ALICE[1], bad)


def test_the_secret_is_only_sent_over_https():
    with pytest.raises(ValueError):
        Chimera(SANDBOX_SECRET, base_url="http://example.com")


def test_the_async_client_does_the_same():
    async def journey(url):
        async with AsyncChimera(SANDBOX_SECRET, base_url=url, pause=0) as chimera:
            await chimera.open_account(*ALICE)
            d = await chimera.create_deposit(ALICE[0], ALICE[1], {"money": "10.00"})
            return d
    with Sandbox() as game:
        d = asyncio.run(journey(game.url))
        game.arrive(d.code, money="10.00")
        with Chimera(SANDBOX_SECRET, base_url=game.url) as chimera:
            assert chimera.get_deposit(d.deposit_id).status == "credited"
