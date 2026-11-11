"""
The clients. `Chimera` and `AsyncChimera` have the same methods. Everything that shapes a request or reads an answer is in `_Calls`, so the two cannot drift apart.

Three promises:

    Amounts are exact. They go out as text and come back as Decimal. A float is refused, because it has already been rounded.
    A retry is safe. A call that creates something carries a request ID, and every retry of it carries the same one, so Chimera acts once. Other calls are reads, or harmless to repeat.
    A secret goes only over HTTPS. Plain HTTP is allowed to this computer only, for the sandbox.
"""
import asyncio
import random
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Mapping, Optional, Sequence, Union
from urllib.parse import urlsplit

import httpx

from .errors import BY_STATUS, ChimeraError, Unavailable

RESOURCES = ("money", "food", "coal", "oil", "uranium", "lead", "iron", "bauxite", "gasoline", "munitions", "steel", "aluminum")
RETRY_STATUSES = (429, 500, 502, 503, 504)
TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 4
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "testserver")
CENTS = re.compile(r"[0-9]{1,28}(\.[0-9]{1,2})?")

Number = Union[Decimal, int, str]


def new_request_id():
    """A fresh ID for a call that creates something. Save it before you make the call, so that after a crash you can repeat the call and not act twice."""
    return "ck-" + secrets.token_urlsafe(24)


def _amount(name, value):
    if name not in RESOURCES:
        raise ValueError("%r is not something Chimera holds." % (name,))
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("%s must be a Decimal, int or str: a float has already been rounded." % name)
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError("%s is not a number." % name)
    if not number.is_finite():
        raise ValueError("%s is not a number." % name)
    text = format(number, "f")
    if not CENTS.fullmatch(text):
        raise ValueError("%s must be positive, with at most two decimal places." % name)
    if not number:
        raise ValueError("%s is zero. Leave it out." % name)
    return text


def _amounts(amounts):
    if not amounts:
        raise ValueError("Give at least one amount.")
    return {k: _amount(k, v) for k, v in amounts.items()}


def _discord(discord_id):
    text = str(discord_id)
    if not re.fullmatch(r"[0-9]{1,20}", text):
        raise ValueError("A Discord ID is digits only.")
    return text


def _money_map(raw):
    return {k: Decimal(str(v)) for k, v in raw.items()}


def _when(text):
    return datetime.fromisoformat(text) if text else None


@dataclass(frozen=True)
class Line:
    """One client's share of a handoff."""
    discord_id: Union[int, str]
    nation_id: int
    amounts: Mapping[str, Number]


@dataclass(frozen=True)
class PendingDeposit:
    deposit_id: int
    amounts: Dict[str, Decimal]
    expires_at: Optional[datetime]


@dataclass(frozen=True)
class Account:
    accountid: int
    discord_id: int
    nation_id: int
    custody: str
    frozen: bool
    balances: Dict[str, Decimal]
    pending_deposits: List[PendingDeposit]


@dataclass(frozen=True)
class Deposit:
    """Send exactly `amounts` from your registered alliance to Chimera, with `code` as the WHOLE note, nothing around it, before `expires_at`. `status` is waiting, credited, expired or under_review."""
    deposit_id: int
    accountid: int
    discord_id: int
    nation_id: int
    alliance_id: int
    code: str
    amounts: Dict[str, Decimal]
    status: str
    created_at: datetime
    expires_at: datetime
    repeat: bool


@dataclass(frozen=True)
class HandoffLine:
    accountid: int
    discord_id: int
    nation_id: int
    amounts: Dict[str, Decimal]


@dataclass(frozen=True)
class Handoff:
    """One transfer for many clients. Send exactly `amounts`, the total, with `code` as the whole note. It credits every line or none. `status` is as for a Deposit."""
    handoff_id: int
    alliance_id: int
    code: str
    amounts: Dict[str, Decimal]
    lines: List[HandoffLine]
    status: str
    created_at: datetime
    expires_at: datetime
    repeat: bool


@dataclass(frozen=True)
class Withdrawal:
    """`status` is processing, sent, completed, failed or under_review. When it is sent, check the in-game record against `code`, pay your client, then call confirm_withdrawal."""
    withdrawal_id: int
    accountid: int
    discord_id: int
    nation_id: int
    alliance_id: int
    code: str
    amounts: Dict[str, Decimal]
    status: str
    created_at: datetime
    repeat: bool


def _account(r):
    pending = [PendingDeposit(p["deposit_id"], _money_map(p["amounts"]), _when(p["expires_at"])) for p in r.get("pending_deposits", [])]
    return Account(int(r["accountid"]), int(r["discord_id"]), r["nation_id"], r["custody"], r["frozen"], _money_map(r["balances"]), pending)


def _deposit(r):
    return Deposit(r["deposit_id"], int(r["accountid"]), int(r["discord_id"]), r["nation_id"], r["alliance_id"], r["code"], _money_map(r["amounts"]), r["status"], _when(r["created_at"]), _when(r["expires_at"]), r["repeat"])


def _handoff(r):
    lines = [HandoffLine(int(x["accountid"]), int(x["discord_id"]), x["nation_id"], _money_map(x["amounts"])) for x in r["lines"]]
    return Handoff(r["handoff_id"], r["alliance_id"], r["code"], _money_map(r["amounts"]), lines, r["status"], _when(r["created_at"]), _when(r["expires_at"]), r["repeat"])


def _withdrawal(r):
    return Withdrawal(r["withdrawal_id"], int(r["accountid"]), int(r["discord_id"]), r["nation_id"], r["alliance_id"], r["code"], _money_map(r["amounts"]), r["status"], _when(r["created_at"]), r["repeat"])


class _Call:
    """One API call: where it goes, what it carries, and how to read a good answer."""

    def __init__(self, method, path, read, body=None, params=None, request_id=None):
        self.method, self.path, self.read, self.body, self.params, self.request_id = method, path, read, body, params, request_id


class _Calls:
    """Every method of both clients, as a description of the call. Each subclass supplies `_do`, which runs one call with its retries."""

    def _do(self, call):
        raise NotImplementedError

    def health(self):
        return self._do(_Call("GET", "/health", lambda r: r["status"] == "ok"))

    def open_account(self, discord_id, nation_id):
        """The client's account, opened if it has none. Calling it twice is harmless. An account starts empty."""
        return self._do(_Call("POST", "/v1/accounts", _account, {"discord_id": _discord(discord_id), "nation_id": int(nation_id)}))

    def accounts(self, after=None, limit=100):
        """One page of your clients' accounts, oldest first, as (accounts, next_after). Pass next_after for the next page. It is None on the last page."""
        params = {"limit": int(limit)}
        if after is not None:
            params["after"] = int(after)
        return self._do(_Call("GET", "/v1/accounts", lambda r: ([_account(a) for a in r["accounts"]], r["next_after"]), params=params))

    def create_deposit(self, discord_id, nation_id, amounts, request_id=None):
        """Says you are about to send these funds, and gets the code for the note. Nothing is credited until Chimera sees the transfer in the game's records."""
        request_id = request_id or new_request_id()
        body = {"request_id": request_id, "discord_id": _discord(discord_id), "nation_id": int(nation_id), "amounts": _amounts(amounts)}
        return self._do(_Call("POST", "/v1/deposits", _deposit, body, request_id=request_id))

    def get_deposit(self, deposit_id):
        return self._do(_Call("GET", "/v1/deposits/%d" % int(deposit_id), _deposit))

    def create_handoff(self, lines: Sequence[Line], request_id=None):
        """One transfer split across many clients, each with an account already. This is the raid tool. Up to 500 lines."""
        request_id = request_id or new_request_id()
        body = {"request_id": request_id, "lines": [{"discord_id": _discord(x.discord_id), "nation_id": int(x.nation_id), "amounts": _amounts(x.amounts)} for x in lines]}
        return self._do(_Call("POST", "/v1/handoffs", _handoff, body, request_id=request_id))

    def get_handoff(self, handoff_id):
        return self._do(_Call("GET", "/v1/handoffs/%d" % int(handoff_id), _handoff))

    def create_withdrawal(self, discord_id, nation_id, amounts, request_id=None, alliance_id=None):
        """Takes the amounts from the client's balance at once. Chimera sends them to `alliance_id` when you give one, and to your registered
        alliance when you do not. Always to an alliance, never to a nation."""
        request_id = request_id or new_request_id()
        body = {"request_id": request_id, "discord_id": _discord(discord_id), "nation_id": int(nation_id), "amounts": _amounts(amounts)}
        if alliance_id is not None:
            body["alliance_id"] = int(alliance_id)
        return self._do(_Call("POST", "/v1/withdrawals", _withdrawal, body, request_id=request_id))

    def get_withdrawal(self, withdrawal_id):
        return self._do(_Call("GET", "/v1/withdrawals/%d" % int(withdrawal_id), _withdrawal))

    def confirm_withdrawal(self, withdrawal_id, code):
        """Says the funds reached your alliance. Saying it twice is harmless."""
        return self._do(_Call("POST", "/v1/withdrawals/%d/confirm" % int(withdrawal_id), _withdrawal, {"code": code}))


def _base_url(url):
    parts = urlsplit(url)
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS):
        raise ValueError("Chimera secrets only go over HTTPS.")
    return url.rstrip("/")


def _headers(secret):
    if not secret:
        raise ValueError("Give your Chimera secret.")
    return {"Authorization": "Bearer %s" % secret, "User-Agent": "chimerakit/0.1"}


def _result(call, response):
    """The answer read, or the right error. None means try again."""
    if response.status_code in (200, 201):
        return call.read(response.json())
    if response.status_code in RETRY_STATUSES:
        return None
    raise _refusal(call, response)


def _refusal(call, response):
    try:
        message = response.json().get("detail") or "Refused."
    except ValueError:
        message = "Refused."
    kind = BY_STATUS.get(response.status_code, Unavailable if response.status_code >= 500 else ChimeraError)
    return kind(str(message), status=response.status_code, request_id=call.request_id)


def _gave_up(call, response, error):
    if response is not None and response.status_code == 429:
        return _refusal(call, response)
    what = "Chimera answered %s." % response.status_code if response is not None else "No answer from Chimera: %s." % type(error).__name__
    return Unavailable(what + " The call may have worked. Repeat it with the same request ID.", status=getattr(response, "status_code", None), request_id=call.request_id)


def _pause(attempt, base):
    return base * (2 ** attempt) * (1 + random.random() / 2)


class Chimera(_Calls):
    """For a blocking program. Use one for the life of your program, and close it, or use `with`. `transport` is for tests."""

    def __init__(self, secret, base_url, timeout=TIMEOUT_SECONDS, attempts=MAX_ATTEMPTS, pause=0.5, transport=None):
        self.attempts, self.pause = attempts, pause
        self._http = httpx.Client(base_url=_base_url(base_url), headers=_headers(secret), timeout=timeout, transport=transport, follow_redirects=False)

    def _do(self, call):
        response = error = None
        for attempt in range(self.attempts):
            if attempt:
                time.sleep(_pause(attempt - 1, self.pause))
            try:
                response, error = self._http.request(call.method, call.path, json=call.body, params=call.params), None
            except httpx.TransportError as e:
                response, error = None, e
                continue
            got = _result(call, response)
            if got is not None:
                return got
        raise _gave_up(call, response, error)

    def iter_accounts(self, page=100):
        """Every account, a page at a time."""
        after = None
        while True:
            accounts, after = self.accounts(after, page)
            yield from accounts
            if after is None:
                return

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class AsyncChimera(_Calls):
    """The same, for a Discord bot or any asyncio program. Every method but iter_accounts and aclose returns something to await."""

    def __init__(self, secret, base_url, timeout=TIMEOUT_SECONDS, attempts=MAX_ATTEMPTS, pause=0.5, transport=None):
        self.attempts, self.pause = attempts, pause
        self._http = httpx.AsyncClient(base_url=_base_url(base_url), headers=_headers(secret), timeout=timeout, transport=transport, follow_redirects=False)

    async def _do(self, call):
        response = error = None
        for attempt in range(self.attempts):
            if attempt:
                await asyncio.sleep(_pause(attempt - 1, self.pause))
            try:
                response, error = await self._http.request(call.method, call.path, json=call.body, params=call.params), None
            except httpx.TransportError as e:
                response, error = None, e
                continue
            got = _result(call, response)
            if got is not None:
                return got
        raise _gave_up(call, response, error)

    async def iter_accounts(self, page=100):
        """Every account, a page at a time."""
        after = None
        while True:
            accounts, after = await self.accounts(after, page)
            for account in accounts:
                yield account
            if after is None:
                return

    async def aclose(self):
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()
