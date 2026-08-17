"""Wire the three real adapters to the conformance FakeVenue and run the laws.

These bindings are what the `_conformance` classmethod would become if it
lived in the adapters themselves. They live here for now so the suite can be
demonstrated against the tree as it stands, without editing production code.

Run:  cd trading-bot && python tests/conformance_drivers.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


from conformance import (Dropped, FakeVenue, HttpStatus, Unreachable,  # noqa: E402
                         run)


# ------------------------------------------------------------------- ccxt
def ccxt_adapter(venue: FakeVenue, read_only=False):
    import ccxt
    from brokers.ccxt_venue import CcxtVenue

    markets = {"BTC/USDT": {"id": "BTCUSDT", "limits": {}, "precision": {},
                            "info": {}, "maker": 0.001, "taker": 0.001}}

    class FakeX:
        apiKey = "k"
        has = {"fetchPositions": False}

        def load_markets(self):
            return markets

        def _list(self):
            rows = []
            for r in venue.list_orders():
                rows.append({"clientOrderId": r.get("client_order_id"),
                             "id": r.get("order_id"), "status": "open",
                             "filled": 0})
            return rows

        def fetch_open_orders(self, sym, limit=None):
            try:
                venue.lookup("__probe__")          # trip the probe faults
            except Unreachable as e:
                raise ccxt.NetworkError(str(e))
            except HttpStatus as e:
                raise ccxt.AuthenticationError(str(e))
            return self._list()

        fetch_closed_orders = fetch_open_orders

        def create_order(self, sym, typ, side, qty, price, params):
            cid = params.get("clientOrderId")
            try:
                row = venue.create(cid, qty=qty)
            except Dropped as e:
                raise ccxt.RequestTimeout(str(e))
            return {"id": row["order_id"], "status": "open",
                    "filled": row.get("filled_quantity") or 0,
                    "average": row.get("avg_filled_price"),
                    "clientOrderId": cid}

        def cancel_order(self, oid, sym=None):
            return venue.cancel(oid)

    v = CcxtVenue({"name": "t", "exchange": "gemini", "read_only": read_only})
    v._x = FakeX()
    v._markets = markets
    return v


# --------------------------------------------------------------- 3commas
def threecommas_adapter(venue: FakeVenue, read_only=False):
    from brokers.threecommas import ThreeCommasVenue

    v = ThreeCommasVenue({"name": "3c", "read_only": read_only, "api_key": "k",
                          "api_secret": "s", "account_id": "A1"})

    closed = set()

    def call(method, path, params=None, body=None):
        if method == "GET" and path == "/ver1/smart_trades/v2":
            try:
                venue.lookup("__probe__")
            except (Unreachable, HttpStatus) as e:
                from brokers import VenueError
                raise VenueError(str(e), retryable=True, venue="3c")
            # Honour page/per_page so the paginated probe is modelled
            # faithfully: a full page means "there may be more".
            per = int((params or {}).get("per_page") or 100)
            page = int((params or {}).get("page") or 1)
            rows = [{"id": r.get("order_id"), "note": r.get("client_order_id"),
                     "pair": "USDT_BTC"} for r in venue.list_orders()]
            return rows[(page - 1) * per: page * per]
        if method == "GET" and path.startswith("/ver1/smart_trades/v2/"):
            # cancel-confirmation read-back
            oid = path.split("/")[-1]
            return {"id": oid, "status": "closed" if oid in closed else "open"}
        if method == "POST" and path == "/ver1/smart_trades/v2":
            cid = (body or {}).get("note")
            try:
                row = venue.create(cid)
            except Dropped as e:
                from brokers import VenueError
                raise VenueError(str(e), retryable=True, venue="3c")
            return {"id": row["order_id"]}
        if "close_by_market" in path:
            oid = path.split("/")[-2]
            res = venue.cancel(oid)      # dict: success + status
            if res.get("success") and str(res.get("status") or "").upper() \
                    in ("CANCELLED", "CLOSED"):
                closed.add(oid)          # a pending/failed cancel does NOT close
            return {}
        return {}

    v._call = call
    return v


# ---------------------------------------------------------------- webull
def webull_adapter(venue: FakeVenue, read_only=False):
    from brokers.webull import WebullVenue

    v = WebullVenue({"name": "wb", "api_key": "K", "api_secret": "S",
                     "account_id": "A1", "instrument_type": "EQUITY",
                     "read_only": read_only})

    def transport(method, url, headers, body):
        parts = urllib.parse.urlsplit(url)
        payload = json.loads(body) if body else None
        date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())

        def out(obj):
            return json.dumps(obj), {"Date": date}

        if parts.path == "/trading/orders/get":
            q = dict(urllib.parse.parse_qsl(parts.query))
            cid = q.get("client_order_id")
            try:
                row = venue.lookup(cid)
            except Unreachable as e:
                raise urllib.error.URLError(str(e))
            except HttpStatus as e:
                raise urllib.error.HTTPError(url, e.code, str(e), {}, None)
            if row is None:
                raise urllib.error.HTTPError(url, 404, "no such order", {}, None)
            return out(row)
        if parts.path == "/trading/orders/place":
            item = payload["new_orders"][0]
            try:
                row = venue.create(item["client_order_id"])
            except Dropped as e:
                raise urllib.error.HTTPError(url, 503, str(e), {}, None)
            return out(row)
        if parts.path == "/trading/orders/cancel":
            return out(venue.cancel(payload.get("order_id")))
        if parts.path == "/trading/accounts/list":
            return out([{"account_id": "A1"}])
        raise urllib.error.HTTPError(url, 404, "no route", {}, None)

    v._transport = transport
    return v


# -------------------------------------------------------------- robinhood
def robinhood_adapter(venue: FakeVenue, read_only=False):
    """Robinhood Crypto uses cursor pagination and a real clientOrderId, so its
    driver models a working cursor over the WHOLE order set — the right test of
    a cursor adapter is whether its probe follows `next` to the end. The order
    list carries client_order_id, and place/cancel echo it back."""
    import base64

    from nacl.signing import SigningKey
    from brokers.robinhood import RobinhoodVenue

    seed = base64.b64encode(bytes(SigningKey.generate())).decode()
    v = RobinhoodVenue({"name": "rh", "api_key": "rh-api-x", "private_key": seed,
                        "read_only": read_only, "account_number": "A1"})

    class Resp:
        def __init__(self, payload, status=200):
            self._p = payload
            self.status_code = status
            self.content = b"x" if payload is not None else b""

        def json(self):
            if self._p is None:
                raise ValueError("no json")
            return self._p

    def request(method, url, headers=None, data=None, timeout=None):
        parts = urllib.parse.urlsplit(url)
        path, q = parts.path, dict(urllib.parse.parse_qsl(parts.query))
        if path.endswith("/accounts/"):
            return Resp({"results": [{"account_number": "A1",
                                      "status": "active", "buying_power": "1000",
                                      "buying_power_currency": "USD"}]})
        if path.endswith("/cancel/"):
            oid = path.rstrip("/").split("/")[-2]
            res = venue.cancel(oid)
            state = "canceled" if (res.get("success") and str(
                res.get("status") or "").upper() in ("CANCELLED", "CLOSED")) \
                else "open"
            return Resp({"id": oid, "state": state})
        if path.endswith("/orders/") and method == "GET":
            try:
                venue.lookup("__probe__")           # trip probe faults
            except Unreachable as e:
                raise __import__("requests").exceptions.ConnectionError(str(e))
            except HttpStatus as e:
                return Resp({"errors": [{"detail": str(e)}]}, status=e.code)
            rows = [{"client_order_id": r.get("client_order_id"),
                     "id": r.get("order_id"), "state": "open",
                     "filled_asset_quantity": "0", "average_price": None}
                    for r in venue.list_orders_full()]
            page = int(q.get("_cur") or 0)
            size = venue.page_size
            chunk = rows[page * size:(page + 1) * size]
            nxt = (url.split("?")[0] + "?_cur=%d" % (page + 1)) \
                if (page + 1) * size < len(rows) else None
            return Resp({"results": chunk, "next": nxt})
        if path.endswith("/orders/") and method == "POST":
            body = json.loads(data.decode()) if data else {}
            try:
                row = venue.create(body.get("client_order_id"))
            except Dropped as e:
                raise __import__("requests").exceptions.ConnectionError(str(e))
            return Resp({"id": row["order_id"],
                         "client_order_id": body.get("client_order_id"),
                         "state": "open",
                         "filled_asset_quantity": row.get("filled_quantity") or "0",
                         "average_price": row.get("avg_filled_price")}, status=201)
        return Resp({"errors": [{"detail": "no route %s" % path}]}, status=404)

    class Session:
        def request(self, *a, **k):
            return request(*a, **k)

    v._session = Session()
    return v


ADAPTERS = [
    ("ccxt (REGISTERED, armable, 103 exchanges)", ccxt_adapter),
    ("3commas (REGISTERED, armable)", threecommas_adapter),
    ("robinhood (REGISTERED, armable)", robinhood_adapter),
    ("webull (disarmed)", webull_adapter),
]

if __name__ == "__main__":
    total = 0
    for name, factory in ADAPTERS:
        print("\n" + "=" * 74)
        print(name)
        print("=" * 74)
        failures = run(name, factory)
        total += len(failures)
        if failures:
            print("\n  %d violation(s):" % len(failures))
            for label, detail in failures:
                print("   -", label)
                print("     " + detail.replace("\n", "\n     ")[:300])
    print("\n" + "=" * 74)
    print("TOTAL VIOLATIONS ACROSS ALL %d ADAPTERS: %d"
          % (len(ADAPTERS), total))
    print("=" * 74)
