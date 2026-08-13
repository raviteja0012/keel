"""Paper trading costs: commission, taker fee, slippage.

Paper P&L used to be pure price movement while live P&L reads profit plus
commission plus swap off the broker deal. The promotion gate reads R-multiples
from that paper P&L, so the gate was certifying strategies on gross numbers and
predicting net results. At a profit factor near 1.1 that difference IS the edge.

These tests pin the behaviour that closes it.
"""
import os
import sys
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TRADING_DB", os.path.join(tempfile.mkdtemp(), "c.db"))

import engine                                              # noqa: E402
import storage                                             # noqa: E402

_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("ok   %s" % name)
    else:
        _failed += 1
        print("FAIL %s %s" % (name, detail))


storage.init()
SYM = "EURUSD"
engine.feed_state["prices"][SYM] = {"tick_size": 0.00001, "tick_value": 0.1,
                                    "bid": 1.10500, "ask": 1.10502,
                                    "src": engine.MT5_SOURCE,
                                    "src_t": time.time()}


def _trade(lots=0.10, entry=1.10000, sl=1.09500):
    return {"id": 0, "symbol": SYM, "lots": lots, "entry": entry,
            "initial_sl": sl, "sl": sl, "tp1_done": 0, "side": "buy",
            "mode": "paper", "trade_mode": "swing"}


def _costs(commission=0.0, taker=0.0, slip=0.0):
    storage.set_setting("paper_commission_per_lot", commission)
    storage.set_setting("paper_taker_fee_pct", taker)
    storage.set_setting("paper_slippage_points", slip)


def test_costs_default_to_non_zero():
    """A cost model that defaults to free is the original bug in a nicer suit."""
    for k in ("paper_commission_per_lot", "paper_taker_fee_pct",
              "paper_slippage_points"):
        storage.execute("DELETE FROM settings WHERE key=?", (k,))
    c = engine.round_turn_cost(_trade(), 1.10500)
    check("unconfigured paper still charges something", c > 0, c)


def test_commission_scales_with_lots():
    _costs(commission=7.0)
    small = engine.round_turn_cost(_trade(lots=0.10), 1.10500)
    big = engine.round_turn_cost(_trade(lots=1.00), 1.10500)
    check("commission is per lot", abs(big - small * 10) < 0.01,
          "%r vs %r" % (small, big))


def test_taker_fee_scales_with_venue_rate():
    """The live fee spread across venues is 12x: kucoin 0.10% to coinbase 1.20%.
    At a thin profit factor that is the difference between an edge and a donation."""
    _costs(taker=0.10)
    cheap = engine.round_turn_cost(_trade(), 1.10500)
    _costs(taker=1.20)
    dear = engine.round_turn_cost(_trade(), 1.10500)
    check("coinbase costs 12x kucoin", abs(dear / cheap - 12.0) < 0.05,
          "%r vs %r" % (cheap, dear))


def test_slippage_charges_both_sides():
    _costs(slip=1.0)
    one = engine.round_turn_cost(_trade(), 1.10500)
    _costs(slip=2.0)
    two = engine.round_turn_cost(_trade(), 1.10500)
    check("slippage is linear and per side", abs(two - one * 2) < 0.001,
          "%r vs %r" % (one, two))


def test_cost_is_never_negative():
    _costs(commission=-100.0, taker=-5.0, slip=-9.0)
    check("a negative cost cannot pay you",
          engine.round_turn_cost(_trade(), 1.10500) == 0.0)


def test_cost_survives_missing_tick_data():
    _costs(commission=7.0, taker=0.1, slip=1.0)
    engine.feed_state["prices"]["NOPRICE"] = {}
    tr = _trade()
    tr["symbol"] = "NOPRICE"
    try:
        c = engine.round_turn_cost(tr, 1.10500)
        check("missing tick data does not raise", c >= 0, c)
    except Exception as e:
        check("missing tick data does not raise", False, repr(e))


def test_close_deducts_cost_and_records_it():
    _costs(commission=7.0, taker=0.0, slip=1.0)
    tid = storage.insert_trade({
        "mode": "paper", "trade_mode": "swing", "symbol": SYM, "side": "buy",
        "status": "open", "grade": "A", "entry_time": 1, "entry": 1.10000,
        "sl": 1.09500, "initial_sl": 1.09500, "tp1": 1.10500, "tp2": 1.11000,
        "lots": 0.10, "risk_pct": 1.0, "risk_amount": 50.0, "setup": "{}",
        "signal_id": 0, "strategy": "slc", "asset_class": "forex"})
    tr = storage.query_one("SELECT * FROM trades WHERE id=?", (tid,))
    gross = (1.10500 - 1.10000) / 0.00001 * 0.1 * 0.10          # 50.0
    engine._close_paper(dict(tr), 1.10500, "take profit (TP2)")
    row = storage.query_one("SELECT pnl, r_multiple, costs FROM trades WHERE id=?", (tid,))
    check("cost recorded on the row", (row["costs"] or 0) > 0, row["costs"])
    check("pnl is net of cost",
          abs(row["pnl"] - (gross - row["costs"])) < 0.02,
          "pnl=%r gross=%r cost=%r" % (row["pnl"], gross, row["costs"]))
    check("net pnl is below gross", row["pnl"] < gross)
    check("R is computed from NET money, so the gate reads net",
          row["r_multiple"] < 1.0, row["r_multiple"])


def test_fees_can_flip_a_marginal_winner_negative():
    """The whole point. A trade that is barely green gross is red net, and the
    gate must see the red one."""
    _costs(commission=0.0, taker=1.20, slip=0.0)     # coinbase-tier fees
    tid = storage.insert_trade({
        "mode": "paper", "trade_mode": "swing", "symbol": SYM, "side": "buy",
        "status": "open", "grade": "A", "entry_time": 1, "entry": 1.10000,
        "sl": 1.09500, "initial_sl": 1.09500, "tp1": 1.10010, "tp2": 1.10020,
        "lots": 0.10, "risk_pct": 1.0, "risk_amount": 50.0, "setup": "{}",
        "signal_id": 0, "strategy": "slc", "asset_class": "forex"})
    tr = storage.query_one("SELECT * FROM trades WHERE id=?", (tid,))
    engine._close_paper(dict(tr), 1.10020, "small win")      # +2.0 gross
    row = storage.query_one("SELECT pnl, r_multiple FROM trades WHERE id=?", (tid,))
    check("a thin gross winner is a net loser at 1.2% fees",
          row["pnl"] < 0, row["pnl"])
    check("and its R is negative too", row["r_multiple"] < 0, row["r_multiple"])


for fn in sorted([f for n, f in list(globals().items()) if n.startswith("test_")],
                 key=lambda f: f.__code__.co_firstlineno):
    fn()

print("\n%d passed, %d failed" % (_passed, _failed))
sys.exit(1 if _failed else 0)
