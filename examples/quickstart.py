"""
A whole journey against the sandbox: run it with `python examples/quickstart.py`. No real funds, and nothing leaves your computer.

The steps marked IN THE GAME are what your bot does with its own bot key. Here the sandbox acts them out.
"""
from decimal import Decimal

from chimerakit import Chimera, Line, new_request_id
from chimerakit.sandbox import SANDBOX_SECRET, Sandbox

ALICE = Line(discord_id="1100000000000000001", nation_id=111, amounts={})
BOB = Line(discord_id="1100000000000000002", nation_id=222, amounts={})


def main():
    with Sandbox() as game, Chimera(SANDBOX_SECRET, base_url=game.url) as chimera:
        # Every client needs an account. Calling this twice is harmless.
        chimera.open_account(ALICE.discord_id, ALICE.nation_id)
        chimera.open_account(BOB.discord_id, BOB.nation_id)

        # A deposit. Save the request ID BEFORE the call, so a crash can repeat the call and not act twice.
        request_id = new_request_id()
        deposit = chimera.create_deposit(ALICE.discord_id, ALICE.nation_id, {"money": Decimal("1000.00")}, request_id=request_id)

        # IN THE GAME: send exactly this from your registered alliance to Chimera, with the code as the whole note.
        game.arrive(deposit.code, money="1000.00")
        print("deposit:", chimera.get_deposit(deposit.deposit_id).status)

        # A raid: one transfer for many clients, one code. It credits every line or none.
        handoff = chimera.create_handoff([Line(ALICE.discord_id, ALICE.nation_id, {"money": "250.00"}), Line(BOB.discord_id, BOB.nation_id, {"money": "750.00", "steel": "10"})])
        game.arrive(handoff.code, money="1000.00", steel="10")           # IN THE GAME: the total, with the code as the whole note
        print("handoff:", chimera.get_handoff(handoff.handoff_id).status)

        # A withdrawal comes off the balance at once, and goes to your alliance, never to a client.
        withdrawal = chimera.create_withdrawal(ALICE.discord_id, ALICE.nation_id, {"money": "500.00"})
        game.send(withdrawal.withdrawal_id)                              # Chimera's worker does this
        print("withdrawal:", chimera.get_withdrawal(withdrawal.withdrawal_id).status)

        # IN THE GAME: find your alliance's incoming transfer, check its note equals withdrawal.code, pay your client, then:
        chimera.confirm_withdrawal(withdrawal.withdrawal_id, withdrawal.code)

        for account in chimera.iter_accounts():
            print(account.discord_id, account.balances["money"], account.balances["steel"])


if __name__ == "__main__":
    main()
