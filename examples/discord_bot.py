"""
How a Discord bot uses the async client. This shows the shape of it and is not a whole bot. `saved` stands for your bot's own database, and `send_to_chimera` for your in-game send.

Keep the secret in your environment, and never in code or a log.
"""
import os

from chimerakit import AsyncChimera, Conflict, LimitExceeded, Unavailable, new_request_id

chimera = AsyncChimera(os.environ["CHIMERA_SECRET"], base_url=os.environ["CHIMERA_URL"])


async def deposit(saved, send_to_chimera, discord_id, nation_id, amounts):
    # Saved before the call: if the bot dies here, it repeats the call with this ID and gets the same code.
    request_id = await saved.new_deposit(discord_id, amounts, new_request_id())
    try:
        made = await chimera.create_deposit(discord_id, nation_id, amounts, request_id=request_id)
    except Conflict as e:
        return "Refused: %s" % e                 # a frozen account, or a deposit already in progress
    except LimitExceeded as e:
        return "Over the limit: %s" % e
    except Unavailable:
        return "Chimera is not answering. Nothing is lost: this will be tried again with the same ID."
    await send_to_chimera(made.amounts, note=made.code)
    return "Sent. It is credited once Chimera sees the transfer in the game's records."


async def check_deposit(deposit_id):
    return (await chimera.get_deposit(deposit_id)).status      # waiting, credited, expired or under_review
