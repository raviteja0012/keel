"""The conformance suite as a CI gate.

conformance.py defines the laws every venue adapter must hold — idempotency
under a dozen faults, no non-finite number on the wire, cancel true only when
confirmed — checked against a hostile fake venue rather than a reviewer's
attention. conformance_drivers.py wires each real adapter to that fake.

This file is what makes the suite bite in CI. It is named test_*.py so the
discovery step runs it, and it asserts two things:

  1. Every ARMABLE (registered) venue passes every law. A regression here is a
     real-money defect — this exact suite already caught three live faults in
     ccxt and four in 3commas that a fully green unit suite had missed.

  2. Webull is NOT registered. Webull fails conformance (by design: it is the
     worked example of why the laws exist), and its safety property is that it
     cannot be built as a venue kind at all. If it ever appears in
     brokers.kinds(), an armable adapter with known double-fill defects just
     went live — so that is a hard failure here.

Run standalone for the full per-adapter report:
    python tests/conformance_drivers.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TRADING_DB", os.path.join(tempfile.mkdtemp(), "cf.db"))

import brokers                                            # noqa: E402
from conformance import run                               # noqa: E402
from conformance_drivers import (ccxt_adapter,            # noqa: E402
                                 robinhood_adapter,
                                 threecommas_adapter,
                                 webull_adapter)

_passed, _failed = 0, 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("ok   %s" % name)
    else:
        _failed += 1
        print("FAIL %s %s" % (name, detail))


# The adapters an operator can actually arm and send real orders through. Every
# one must pass every law. Add a venue here the moment it becomes registerable.
ARMABLE = [
    ("ccxt", ccxt_adapter),
    ("3commas", threecommas_adapter),
    ("robinhood", robinhood_adapter),
]


def test_every_armable_adapter_passes_every_law():
    for kind, factory in ARMABLE:
        failures = run(kind, factory, verbose=False)
        check("%s: 0 conformance violations" % kind, not failures,
              "; ".join(f[0] for f in failures))


def test_armable_adapters_are_actually_registered():
    # If a kind we assert conformance for is not registered, the gate is
    # guarding a venue nobody can use — and, worse, a NEW registered venue with
    # no conformance coverage would slip through unnoticed.
    registered = set(brokers.kinds())
    for kind, _ in ARMABLE:
        check("%s is registered" % kind, kind in registered, registered)


def test_every_registered_venue_has_conformance_coverage():
    # The gate must not fall behind reality. If someone registers a new venue
    # kind and does not add it to ARMABLE, this fails and tells them to.
    covered = {k for k, _ in ARMABLE}
    # mt5 is the EA bridge, not a brokers.build() venue; exclude if present.
    for kind in brokers.kinds():
        if kind in ("mt5",):
            continue
        check("registered venue %r is covered by the conformance gate" % kind,
              kind in covered,
              "add it to ARMABLE in test_conformance.py")


def test_webull_is_not_registered():
    # Webull's safety property is non-existence as a venue kind. It fails
    # conformance on purpose; the guarantee is that it cannot be armed.
    check("webull is NOT a buildable venue kind",
          "webull" not in brokers.kinds(), brokers.kinds())


def test_webull_still_fails_conformance_as_documented():
    # Belt and suspenders: if webull were ever silently "fixed" enough to pass,
    # someone should consciously re-arm it, not have it drift back in.
    failures = run("webull", webull_adapter, verbose=False)
    check("webull still fails conformance (why it stays disarmed)",
          len(failures) > 0, "webull now passes — reconsider disarming")


for fn in sorted([f for n, f in list(globals().items()) if n.startswith("test_")],
                 key=lambda f: f.__code__.co_firstlineno):
    fn()

print("\n%d passed, %d failed" % (_passed, _failed))
sys.exit(1 if _failed else 0)
