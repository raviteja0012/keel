"""research_feed: parsing a research newsletter into structured facts.

The parser is pure, so these run with no mailbox, no network and no credentials.
The HTML below is shaped like a real Finimize daily brief, including the two
things that actually broke the first implementation: label lines that carry
their own digits (S&P 500, FTSE 100) and a footnote that does the same
(*10-year government yield).
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TRADING_DB", os.path.join(tempfile.mkdtemp(), "t.db"))

import storage                                              # noqa: E402
import research_feed as rf                                  # noqa: E402

_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("ok   %s" % name)
    else:
        _failed += 1
        print("FAIL %s %s" % (name, detail))


BRIEF = """
<html><body>
<a href="x">View in browser</a>
<div>WHAT'S GOING ON</div>
<div>S&amp;P 500</div><div>0.59% &#9650;</div><div>7,756</div>
<div>GOLD</div><div>2.26% &#9650;</div><div>$4,397</div>
<div>BITCOIN*</div><div>1.03% &#9650;</div><div>$64,920</div>
<div>*24-hour change</div>
<div>FTSE 100</div><div>0.38% &#9650;</div><div>10,909</div>
<div>BRENT OIL</div><div>0.23% &#9660;</div><div>$82.30</div>
<div>US BONDS*</div><div>0.02% &#9660;</div><div>4.65%</div>
<div>*10-year government yield</div>
<p>The US economy unexpectedly shed 23,000 jobs last month, so the Federal
Reserve's next steps likely will not be as clean-cut as it had hoped for.</p>
<a href="y">Unsubscribe</a>
</body></html>
"""


def _snap(note):
    return {r["label"]: r for r in note["snapshot"]}


def test_source_is_recognised_from_the_sender():
    n = rf.parse_email("hello@finimize.com", "Daily Brief", BRIEF)
    check("source recognised", n["source"] == "finimize", n["source"])
    n2 = rf.parse_email("someone@nowhere.test", "x", BRIEF)
    check("unknown sender is not guessed at", n2["source"] == "unknown", n2["source"])


def test_every_instrument_parses_level_and_change():
    s = _snap(rf.parse_email("hello@finimize.com", "d", BRIEF))
    want = {"S&P 500": (7756.0, 0.59), "GOLD": (4397.0, 2.26),
            "BITCOIN": (64920.0, 1.03), "FTSE 100": (10909.0, 0.38),
            "BRENT OIL": (82.30, -0.23), "US BONDS": (4.65, -0.02)}
    for label, (lvl, chg) in want.items():
        row = s.get(label)
        check("parsed %s" % label, row is not None)
        if not row:
            continue
        check("%s level" % label, abs((row["level"] or 0) - lvl) < 0.011,
              "got %r want %r" % (row["level"], lvl))
        check("%s change" % label, abs((row["change_pct"] or 0) - chg) < 0.011,
              "got %r want %r" % (row["change_pct"], chg))


def test_label_digits_do_not_leak_into_the_level():
    """The regression that shipped: 'FTSE 100' and '*10-year government yield'
    contain digits, and a max-of-window heuristic picked them. US BONDS came out
    as 10.0 instead of the 4.65% yield."""
    s = _snap(rf.parse_email("hello@finimize.com", "d", BRIEF))
    check("US BONDS is the yield, not the '10' in 10-year",
          abs(s["US BONDS"]["level"] - 4.65) < 0.001, s["US BONDS"]["level"])
    check("FTSE is the index, not the '100' in its name",
          abs(s["FTSE 100"]["level"] - 10909.0) < 0.001, s["FTSE 100"]["level"])


def test_direction_marker_sets_the_sign():
    s = _snap(rf.parse_email("hello@finimize.com", "d", BRIEF))
    check("up arrow is positive", s["GOLD"]["change_pct"] > 0)
    check("down arrow is negative", s["BRENT OIL"]["change_pct"] < 0)


def test_narrative_is_kept_apart_from_the_numbers():
    n = rf.parse_email("hello@finimize.com", "d", BRIEF)
    joined = " ".join(n["narrative"])
    check("prose captured", "23,000 jobs" in joined)
    check("prose is not in the snapshot",
          all("jobs" not in str(r) for r in n["snapshot"]))


def test_boilerplate_is_dropped():
    n = rf.parse_email("hello@finimize.com", "d", BRIEF)
    joined = " ".join(n["narrative"]).lower()
    check("unsubscribe dropped", "unsubscribe" not in joined)
    check("view in browser dropped", "view in browser" not in joined)


def test_garbage_input_does_not_raise():
    for bad in ("", None, "<html>", "no tags at all", "<div>,</div><div>,,</div>"):
        try:
            n = rf.parse_email("hello@finimize.com", "s", bad)
            check("survives %r" % (bad if bad else "empty"), isinstance(n, dict))
        except Exception as e:
            check("survives %r" % (bad if bad else "empty"), False, repr(e))


def test_record_is_idempotent_for_the_same_brief():
    storage.init()
    n = rf.parse_email("hello@finimize.com", "Same Brief", BRIEF)
    rf.record(n)
    rf.record(n)
    rows = storage.query(
        "SELECT COUNT(*) c FROM research_notes WHERE subject='Same Brief'")
    check("a resent newsletter is stored once", rows[0]["c"] == 1, rows[0]["c"])


def test_stale_snapshot_is_reported_absent_not_stale_data():
    """A three-day-old index level presented as current context is exactly the
    quiet wrongness the integrity checks exist to catch."""
    storage.execute("DELETE FROM research_notes")
    old = rf.parse_email("hello@finimize.com", "Old", BRIEF)
    old["received_t"] = int(time.time()) - 5 * 86400
    rf.record(old)
    got = rf.latest_snapshot(max_age_h=36)
    check("stale brief yields no rows", got["stale"] is True and not got["rows"],
          got.get("reason"))

    fresh = rf.parse_email("hello@finimize.com", "Fresh", BRIEF)
    rf.record(fresh)
    got = rf.latest_snapshot(max_age_h=36)
    check("fresh brief yields rows", got["stale"] is False and len(got["rows"]) == 6,
          len(got.get("rows", [])))


def test_module_never_trades():
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "research_feed.py")).read()
    for forbidden in ("place_order", "enqueue_command", "try_execute",
                      "insert_trade", "update_trade"):
        check("research_feed does not call %s" % forbidden, forbidden not in src)


for fn in sorted([f for n, f in list(globals().items()) if n.startswith("test_")],
                 key=lambda f: f.__code__.co_firstlineno):
    fn()

print("\n%d passed, %d failed" % (_passed, _failed))
sys.exit(1 if _failed else 0)
