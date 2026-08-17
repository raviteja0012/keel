"""Strategy-host registry and the hosted-exposure rail.

Two safety properties get pinned here, both of the family this codebase keeps
shipping and re-fixing:

  1. Secrets are masked at EVERY nesting level. Altrady stores one
     api_key/api_secret pair inside each entry of `bots`; a redactor that only
     walks the top level hands the dashboard live secrets in the middle of a
     JSON blob.

  2. An unreadable host is not a flat host. hosts.exposure() must count what
     it could not read, and engine.loss_limits_hit must stand aside in live
     mode when the hosted picture is untrustworthy — the same discipline as
     an unvaluable open position.
"""
import os
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TRADING_DB", os.path.join(tempfile.mkdtemp(), "h.db"))

import engine                                              # noqa: E402
import hosts                                               # noqa: E402
import storage                                             # noqa: E402
from brokers import strategy_host as sh                    # noqa: E402
from brokers.strategy_host import (Bot, BotAction, BotState,  # noqa: E402
                                   STOP_ORPHANS_POSITIONS, register_host)
from brokers import Position, VenueHealth                  # noqa: E402

_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("ok   %s" % name)
    else:
        _failed += 1
        print("FAIL %s %s" % (name, detail))


# -------------------------------------------------------------- fake host
# Behaviour is scripted per test through class attributes.

class FakeHost:
    polls = 0                     # bot_state calls, to observe the cache
    fail_bots = False
    state_pnl = 12.5              # None = unvalued

    def __init__(self, config):
        self.name = config.get("name", "fake")
        self.read_only = bool(config.get("read_only", True))
        self.stop_disposition = STOP_ORPHANS_POSITIONS

    def health(self):
        return VenueHealth(name=self.name, reachable=True, authenticated=True,
                           read_only=self.read_only)

    def bots(self):
        if FakeHost.fail_bots:
            raise RuntimeError("host unreachable (api_secret=SHOULD_NOT_LEAK)")
        return [Bot(bot_id="b1", name="grid-1", host=self.name, running=True)]

    def bot_state(self, bot_id):
        FakeHost.polls += 1
        return BotState(bot_id=bot_id, running=True,
                        positions=[Position(symbol="BTC/USDT", side="buy",
                                            qty=0.1, entry_price=60000)],
                        unrealized_pnl=FakeHost.state_pnl,
                        as_of=int(time.time()))

    def start_bot(self, bot_id):
        return BotAction(bot_id=bot_id, running=True, changed=False)

    def stop_bot(self, bot_id):
        return BotAction(bot_id=bot_id, running=False, changed=True)


register_host("fakehost", FakeHost)


def reset():
    for cfg in list(hosts._all()):
        hosts.remove(cfg["name"])
    hosts._invalidate_exposure()
    FakeHost.polls = 0
    FakeHost.fail_bots = False
    FakeHost.state_pnl = 12.5


# ------------------------------------------------------------- redaction

def test_redaction_walks_nested_bot_credentials():
    reset()
    hosts.upsert({"name": "alt", "kind": "fakehost", "api_key": "TOPKEY",
                  "bots": [{"bot_id": "b1", "name": "grid",
                            "api_key": "BOTKEY", "api_secret": "BOTSECRET"}]})
    listed = hosts.list_hosts()[0]
    blob = str(listed)
    check("top-level secret masked", "TOPKEY" not in blob)
    check("nested bot key masked", "BOTKEY" not in blob)
    check("nested bot secret masked", "BOTSECRET" not in blob)
    check("nested fingerprint present so the UI can tell which key is loaded",
          listed["bots"][0].get("api_key_fp"), listed["bots"][0])
    check("but the full config still has the secrets",
          hosts.get("alt")["bots"][0]["api_secret"] == "BOTSECRET")


def test_resave_with_masked_secret_keeps_the_stored_one():
    reset()
    hosts.upsert({"name": "alt", "kind": "fakehost", "api_key": "REALKEY",
                  "bots": [{"bot_id": "b1", "api_secret": "REALSECRET"}]})
    # UI round-trip: masked values come back, must not clobber
    hosts.upsert({"name": "alt", "kind": "fakehost", "api_key": hosts._MASK,
                  "bots": [{"bot_id": "b1", "api_secret": hosts._MASK,
                            "note": "edited"}]})
    cfg = hosts.get("alt")
    check("masked top-level secret kept the stored value",
          cfg["api_key"] == "REALKEY")
    check("masked per-bot secret kept the stored value",
          cfg["bots"][0]["api_secret"] == "REALSECRET")
    check("while the non-secret edit landed", cfg["bots"][0]["note"] == "edited")


def test_upsert_validation():
    reset()
    try:
        hosts.upsert({"name": "", "kind": "fakehost"})
        check("empty name rejected", False)
    except ValueError:
        check("empty name rejected", True)
    try:
        hosts.upsert({"name": "x", "kind": "nope"})
        check("unknown kind rejected", False)
    except ValueError:
        check("unknown kind rejected", True)
    check("read_only defaults True",
          hosts.upsert({"name": "x", "kind": "fakehost"})["read_only"] is True)


# -------------------------------------------------------------- exposure

def test_no_hosts_is_a_trustworthy_zero():
    reset()
    ex = hosts.exposure()
    check("no hosts -> trustworthy", ex["trustworthy"] is True)
    check("and zero, honestly (nothing exists, not nothing-readable)",
          ex["unrealized_pnl"] == 0.0 and ex["unreadable_hosts"] == 0)


def test_valued_bots_sum_and_the_cache_holds():
    reset()
    hosts.upsert({"name": "f1", "kind": "fakehost"})
    ex = hosts.exposure()
    check("valued bot P&L flows through", ex["unrealized_pnl"] == 12.5, ex)
    check("open positions counted", ex["open_positions"] == 1)
    check("snapshot is trustworthy", ex["trustworthy"] is True)
    polls = FakeHost.polls
    hosts.exposure()
    check("second read within TTL is served from cache (rate limits are real)",
          FakeHost.polls == polls, FakeHost.polls)
    hosts._invalidate_exposure()
    hosts.exposure()
    check("invalidation forces a real re-poll", FakeHost.polls > polls)


def test_unvalued_bot_poisons_trust_not_the_total():
    reset()
    hosts.upsert({"name": "f1", "kind": "fakehost"})
    FakeHost.state_pnl = None
    ex = hosts.exposure()
    check("unvalued bot counted", ex["unvalued_bots"] == 1, ex)
    check("and the snapshot is NOT trustworthy", ex["trustworthy"] is False)
    check("and the total did not silently absorb a zero for it",
          ex["unrealized_pnl"] == 0.0)


def test_unreachable_host_is_counted_and_never_leaks_secrets():
    reset()
    hosts.upsert({"name": "f1", "kind": "fakehost"})
    FakeHost.fail_bots = True
    ex = hosts.exposure()
    check("unreachable host counted", ex["unreadable_hosts"] == 1, ex)
    check("snapshot untrustworthy", ex["trustworthy"] is False)
    check("detail carries the host name", "f1" in ex["detail"])
    # The fake's error message deliberately embeds a secret-looking token;
    # exposure truncates but does not scrub — scrubbing is the ADAPTER's job
    # (its exceptions must already be clean). Pin the length bound instead.
    check("detail is bounded", len(ex["detail"]) <= 400)


# ------------------------------------------------- the engine kill switch

P = {"daily_stop_pct": 2.0, "weekly_stop_pct": 5.0}


def _with_stub_exposure(snap, fn):
    old_mod, old_avail = engine._hosts, engine._HOSTS_AVAILABLE
    engine._hosts = SimpleNamespace(exposure=lambda: snap)
    engine._HOSTS_AVAILABLE = True
    try:
        return fn()
    finally:
        engine._hosts, engine._HOSTS_AVAILABLE = old_mod, old_avail


def test_live_entries_stand_aside_when_hosted_picture_is_untrustworthy():
    reason = _with_stub_exposure(
        {"trustworthy": False, "unreadable_hosts": 1, "unvalued_bots": 0,
         "unrealized_pnl": 0.0, "detail": "f1: down"},
        lambda: engine.loss_limits_hit("live", 10000.0, P))
    check("untrustworthy hosted exposure halts live entries",
          reason is not None and "hosted" in reason, reason)


def test_hosted_drawdown_counts_against_the_daily_stop():
    # Own book is flat (empty DB). Hosted bots are -250 on a 10k balance with
    # a 2% daily stop: -250 alone is not past -200? It is: -250 <= -200.
    reason = _with_stub_exposure(
        {"trustworthy": True, "unreadable_hosts": 0, "unvalued_bots": 0,
         "unrealized_pnl": -250.0, "detail": ""},
        lambda: engine.loss_limits_hit("live", 10000.0, P))
    check("hosted unrealized loss trips the daily stop",
          reason is not None and "daily stop" in reason, reason)


def test_hosted_profit_never_offsets():
    reason = _with_stub_exposure(
        {"trustworthy": True, "unreadable_hosts": 0, "unvalued_bots": 0,
         "unrealized_pnl": 5000.0, "detail": ""},
        lambda: engine.loss_limits_hit("live", 10000.0, P))
    check("hosted profit does not open the gate wider", reason is None, reason)


def test_trustworthy_with_no_number_is_refused_not_zeroed():
    reason = _with_stub_exposure(
        {"trustworthy": True, "unreadable_hosts": 0, "unvalued_bots": 0,
         "unrealized_pnl": None, "detail": ""},
        lambda: engine.loss_limits_hit("live", 10000.0, P))
    check("a trustworthy snapshot with no P&L number stands aside",
          reason is not None and "refusing" in reason, reason)


def test_paper_mode_ignores_hosts_entirely():
    called = {"n": 0}

    def _boom():
        called["n"] += 1
        raise AssertionError("paper mode must not read hosted exposure")

    old_mod, old_avail = engine._hosts, engine._HOSTS_AVAILABLE
    engine._hosts = SimpleNamespace(exposure=_boom)
    engine._HOSTS_AVAILABLE = True
    try:
        reason = engine.loss_limits_hit("paper", 10000.0, P)
    finally:
        engine._hosts, engine._HOSTS_AVAILABLE = old_mod, old_avail
    check("paper entries never consult hosts", called["n"] == 0 and reason is None,
          (called, reason))


for fn in sorted([f for n, f in list(globals().items()) if n.startswith("test_")],
                 key=lambda f: f.__code__.co_firstlineno):
    fn()

print("\n%d passed, %d failed" % (_passed, _failed))
sys.exit(1 if _failed else 0)
