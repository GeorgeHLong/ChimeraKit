"""
A pretend Chimera on your own computer. It holds no real funds, reaches nothing outside your computer, and forgets everything when it stops.

    python -m chimerakit.sandbox

    Chimera(SANDBOX_SECRET, base_url="http://127.0.0.1:8099")

In real use the funds move in the game. Here you act that out by hand:

    POST /sandbox/arrive    {"code": "...", "money": "100.00"}     the in-game transfer reaches Chimera. Add "note" to send a wrong note, and "alliance_id" to send from the wrong alliance
    POST /sandbox/send      {"withdrawal_id": 1}                   Chimera sends a withdrawal to your alliance

In Python, use the same two steps directly: `sandbox.arrive(code, money="100.00")` and `sandbox.send(withdrawal_id)`.

Every answer has the same shape and status as the real API, and a test in Chimera's own repository holds the two together. Set `transfer_limit` and `daily_limit` to try your limit errors.
"""
import argparse
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .client import CENTS, RESOURCES

SANDBOX_SECRET = "chim_sandbox_not-a-real-secret"
ALLIANCE_ID = 1000
CODE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
WINDOW_MINUTES = 30
MAX_PAGE = 500
MAX_LINES = 500


class _Refused(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _text(amounts):
    return {k: str(v) for k, v in amounts.items()}


def _parse(amounts):
    if not isinstance(amounts, dict) or not amounts:
        raise _Refused(422, "Enter an amount.")
    out = {}
    for k, v in amounts.items():
        if k not in RESOURCES or not isinstance(v, str) or not CENTS.fullmatch(v) or not Decimal(v):
            raise _Refused(422, "Invalid request.")
        out[k] = Decimal(v)
    return out


class Sandbox:
    """The pretend bank and its web server. Use `with Sandbox() as s:` and give a client s.url."""

    def __init__(self, port=0, alliance_id=ALLIANCE_ID, transfer_limit=Decimal(10) ** 12, daily_limit=Decimal(10) ** 13):
        self.alliance_id, self.transfer_limit, self.daily_limit = alliance_id, Decimal(transfer_limit), Decimal(daily_limit)
        self.lock = threading.RLock()
        self.accounts, self.requests, self.used, self.next_id = {}, {}, {}, 1
        self.server = ThreadingHTTPServer(("127.0.0.1", port), _handler(self))
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()

    def _id(self):
        self.next_id += 1
        return self.next_id - 1

    def _code(self):
        return "".join(secrets.choice(CODE_LETTERS) for _ in range(16))

    def _account_of(self, discord_id, nation_id, message="That account was not found."):
        a = self.accounts.get(int(discord_id))
        if a is None or a["nation_id"] != nation_id:
            raise _Refused(404, message)
        return a

    def _limits(self, kind, amounts):
        if any(v > self.transfer_limit for v in amounts.values()):
            raise _Refused(403, "That is over this bank's limit for one transfer.")
        if any(sum((r["amounts"].get(k, 0) for r in self.requests.values() if r["group"] == kind), Decimal(0)) + v > self.daily_limit for k, v in amounts.items()):
            raise _Refused(403, "That is over this bank's limit for the day.")

    def _status(self, r):
        return r["status"]

    def _account_view(self, a, with_pending=False):
        view = {"accountid": str(a["accountid"]), "discord_id": str(a["discord_id"]), "nation_id": a["nation_id"], "custody": "chimera", "frozen": a["frozen"], "balances": {k: str(a["balances"][k]) for k in RESOURCES}}
        if with_pending:
            view["pending_deposits"] = [{"deposit_id": r["id"], "amounts": _text(amounts), "expires_at": r["expires_at"].isoformat()} for r in self.requests.values() if r["status"] == "waiting"
                                        for acct, amounts in r["lines"] if acct is a]
        return view

    def _deposit_view(self, r, repeat=False):
        a = r["lines"][0][0]
        return {"deposit_id": r["id"], "accountid": str(a["accountid"]), "discord_id": str(a["discord_id"]), "nation_id": a["nation_id"], "alliance_id": self.alliance_id, "code": r["code"],
                "amounts": _text(r["amounts"]), "status": r["status"], "created_at": r["created_at"].isoformat(), "expires_at": r["expires_at"].isoformat(), "repeat": repeat}

    def _handoff_view(self, r, repeat=False):
        lines = [{"accountid": str(a["accountid"]), "discord_id": str(a["discord_id"]), "nation_id": a["nation_id"], "amounts": _text(m)} for a, m in r["lines"]]
        return {"handoff_id": r["id"], "alliance_id": self.alliance_id, "code": r["code"], "amounts": _text(r["amounts"]), "lines": lines, "status": r["status"],
                "created_at": r["created_at"].isoformat(), "expires_at": r["expires_at"].isoformat(), "repeat": repeat}

    def _withdrawal_view(self, r, repeat=False):
        a = r["lines"][0][0]
        return {"withdrawal_id": r["id"], "accountid": str(a["accountid"]), "discord_id": str(a["discord_id"]), "nation_id": a["nation_id"],
                "alliance_id": r.get("destination") or self.alliance_id, "code": r["code"],
                "amounts": _text(r["amounts"]), "status": r["status"], "created_at": r["created_at"].isoformat(), "repeat": repeat}

    def _new(self, kind, group, lines, amounts, key):
        now = datetime.now(timezone.utc)
        r = {"id": self._id(), "kind": kind, "group": group, "lines": lines, "amounts": amounts, "code": self._code(), "status": "waiting" if group == "deposit" else "processing", "key": key,
             "created_at": now, "expires_at": now + timedelta(minutes=WINDOW_MINUTES), "confirmed": False}
        self.requests[r["id"]] = r
        return r

    def _repeat(self, key):
        return next((r for r in self.requests.values() if r["key"] == key), None)

    def _find(self, kind, request_id, message):
        r = self.requests.get(request_id)
        if r is None or r["kind"] != kind:
            raise _Refused(404, message)
        return r

    # -- the API --------------------------------------------------------------
    def open_account(self, body):
        d, nation = int(body["discord_id"]), body["nation_id"]
        a = self.accounts.get(d)
        if a is not None and a["nation_id"] != nation or any(x["nation_id"] == nation and x["discord_id"] != d for x in self.accounts.values()):
            raise _Refused(422, "That Discord ID and nation do not match this bank's records.")
        if a is None:
            a = self.accounts[d] = {"accountid": 1000000 + len(self.accounts), "discord_id": d, "nation_id": nation, "frozen": False, "balances": {k: Decimal("0.00") for k in RESOURCES}}
        return self._account_view(a)

    def list_accounts(self, after, limit):
        rows = sorted((a for a in self.accounts.values() if a["accountid"] > after), key=lambda a: a["accountid"])[:limit]
        return {"accounts": [self._account_view(a, True) for a in rows], "next_after": str(rows[-1]["accountid"]) if len(rows) == limit else None}

    def create_deposit(self, body):
        amounts, key = _parse(body["amounts"]), "d:" + body["request_id"]
        again = self._repeat(key)
        if again:
            a = again["lines"][0][0]
            if (str(a["discord_id"]), a["nation_id"]) != (body["discord_id"], body["nation_id"]) or again["amounts"] != amounts:
                raise _Refused(409, "That request ID was already used for a different deposit.")
            return self._deposit_view(again, True)
        a = self._account_of(body["discord_id"], body["nation_id"])
        if a["frozen"]:
            raise _Refused(409, "That account is frozen.")
        self._limits("deposit", amounts)
        if any(r["kind"] == "deposit" and r["status"] in ("waiting", "under_review") and r["lines"][0][0] is a for r in self.requests.values()):
            raise _Refused(409, "That client already has a deposit in progress.")
        return self._deposit_view(self._new("deposit", "deposit", [(a, amounts)], amounts, key))

    def create_handoff(self, body):
        wanted, total = [], {}
        lines = body["lines"]
        if len({x["discord_id"] for x in lines}) != len(lines):
            raise _Refused(422, "A client can only appear once in a handoff.")
        for x in lines:
            m = _parse(x["amounts"])
            wanted.append((x, m))
            for k, v in m.items():
                total[k] = total.get(k, Decimal(0)) + v
        key = "h:" + body["request_id"]
        again = self._repeat(key)
        if again:
            if {(a["discord_id"], tuple(sorted(m.items()))) for a, m in again["lines"]} != {(int(x["discord_id"]), tuple(sorted(m.items()))) for x, m in wanted}:
                raise _Refused(409, "That request ID was already used for a different handoff.")
            return self._handoff_view(again, True)
        accounts = []
        for x, m in wanted:
            a = self._account_of(x["discord_id"], x["nation_id"], "The account for client %s was not found." % x["discord_id"])
            if a["frozen"]:
                raise _Refused(409, "The account for client %s is frozen." % x["discord_id"])
            accounts.append((a, m))
        self._limits("deposit", total)
        return self._handoff_view(self._new("handoff", "deposit", accounts, total, key))

    def create_withdrawal(self, body):
        amounts, key = _parse(body["amounts"]), "w:" + body["request_id"]
        destination = body.get("alliance_id")
        if destination is not None and (not isinstance(destination, int) or isinstance(destination, bool) or destination <= 0):
            raise _Refused(422, "Invalid request.")
        again = self._repeat(key)
        if again:
            a = again["lines"][0][0]
            if ((str(a["discord_id"]), a["nation_id"]) != (body["discord_id"], body["nation_id"]) or again["amounts"] != amounts
                    or (destination is not None and (again.get("destination") or self.alliance_id) != destination)):
                raise _Refused(409, "That request ID was already used for a different withdrawal.")
            return self._withdrawal_view(again, True)
        a = self._account_of(body["discord_id"], body["nation_id"])
        if a["frozen"]:
            raise _Refused(409, "That account is frozen.")
        self._limits("withdraw", amounts)
        if any(a["balances"][k] < v for k, v in amounts.items()):
            raise _Refused(409, "That account does not hold enough.")
        for k, v in amounts.items():
            a["balances"][k] -= v
        r = self._new("withdrawal", "withdraw", [(a, amounts)], amounts, key)
        r["destination"] = destination
        return self._withdrawal_view(r)

    def confirm_withdrawal(self, withdrawal_id, code):
        r = self._find("withdrawal", withdrawal_id, "That withdrawal was not found.")
        if r["code"] != code:
            raise _Refused(409, "That code does not match this withdrawal.")
        if r["status"] not in ("sent", "completed"):
            raise _Refused(409, "That withdrawal has not been sent.")
        view = self._withdrawal_view(r)                 # as on the real API: this answer shows what was true when the bank called
        r["status"] = "completed"
        return view

    # -- acting out the game ----------------------------------------------------
    def arrive(self, code, note=None, alliance_id=None, **amounts):
        """The in-game transfer reaches Chimera. Returns what happened: credited, or under_review with the reason the real worker would give."""
        with self.lock:
            r = next((x for x in self.requests.values() if x["code"] == code and x["group"] == "deposit"), None)
            if r is None:
                raise _Refused(404, "No deposit has that code.")
            sent = {k: Decimal(str(v)) for k, v in amounts.items()}
            why = ("The funds came from another alliance." if alliance_id not in (None, self.alliance_id) else "The note is not exactly the code." if (code if note is None else note) != code
                   else "This code was already used." if r["status"] == "credited" else "The amounts sent do not match the request." if sent != r["amounts"] else None)
            if why:
                r["status"] = "under_review" if r["status"] != "credited" else r["status"]
                return {"status": "under_review", "reason": why}
            for a, m in r["lines"]:
                for k, v in m.items():
                    a["balances"][k] += v
            r["status"] = "credited"
            return {"status": "credited", "reason": None}

    def send(self, withdrawal_id):
        """Chimera sends the withdrawal. Check `code` against your in-game record, then confirm."""
        with self.lock:
            r = self._find("withdrawal", withdrawal_id, "That withdrawal was not found.")
            r["status"] = "sent"
            return {"status": "sent", "code": r["code"]}


def _handler(box):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _answer(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self, method, path, query, body):
            if path == "/health":
                return 200, {"status": "ok"}
            if path == "/sandbox/arrive":
                kw = dict(body)
                return 200, box.arrive(kw.pop("code"), **kw)
            if path == "/sandbox/send":
                return 200, box.send(body["withdrawal_id"])
            if self.headers.get("Authorization") != "Bearer " + SANDBOX_SECRET:
                raise _Refused(401, "Not authorized.")
            if (method, path) == ("POST", "/v1/accounts"):
                return 201, box.open_account(body)
            if (method, path) == ("GET", "/v1/accounts"):
                limit = int(query.get("limit", ["100"])[0])
                if not 1 <= limit <= MAX_PAGE:
                    raise _Refused(422, "Invalid request.")
                return 200, box.list_accounts(int(query.get("after", ["0"])[0]), limit)
            if (method, path) == ("POST", "/v1/deposits"):
                return 201, box.create_deposit(body)
            if (method, path) == ("POST", "/v1/handoffs"):
                if not 1 <= len(body.get("lines", [])) <= MAX_LINES:
                    raise _Refused(422, "Invalid request.")
                return 201, box.create_handoff(body)
            if (method, path) == ("POST", "/v1/withdrawals"):
                return 201, box.create_withdrawal(body)
            parts = path.strip("/").split("/")
            if method == "GET" and len(parts) == 3 and parts[0] == "v1" and parts[2].isdigit():
                found = {"deposits": ("deposit", box._deposit_view, "That deposit was not found."), "handoffs": ("handoff", box._handoff_view, "That handoff was not found."),
                         "withdrawals": ("withdrawal", box._withdrawal_view, "That withdrawal was not found.")}.get(parts[1])
                if found:
                    return 200, found[1](box._find(found[0], int(parts[2]), found[2]))
            if method == "POST" and len(parts) == 4 and parts[:2] == ["v1", "withdrawals"] and parts[3] == "confirm" and parts[2].isdigit():
                return 200, box.confirm_withdrawal(int(parts[2]), body.get("code"))
            raise _Refused(404, "Not Found")

        def _serve(self, method):
            url = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                with box.lock:
                    status, answer = self._route(method, url.path, parse_qs(url.query), body)
            except _Refused as e:
                status, answer = e.status, {"detail": e.detail}
            except (KeyError, TypeError, ValueError):
                status, answer = 422, {"detail": "Invalid request."}
            self._answer(status, answer)

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

    return Handler


def main():
    parser = argparse.ArgumentParser(description="A pretend Chimera on your own computer. No real funds.")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    box = Sandbox(args.port)
    print("Sandbox at %s\nTest secret: %s\nPress Ctrl+C to stop. Everything is forgotten when it stops." % (box.url, SANDBOX_SECRET), flush=True)
    try:
        box.server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
