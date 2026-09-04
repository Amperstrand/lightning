"""Audit item D, restart arm: is the wallet_utxo_boost abort a CRASH
LOOP across restarts?

Claim under test (lightning-playground
docs/upstream-drafts/2026-09-04-cln-utxo-boost-abort.md, quoted; the
draft states it as hypothesis, "restart-loop verification pending"):

    On restart the anchor attempt re-runs against the same UTXO
    (crash loop until the deadline window passes)

Crash #1 is already machine-proven by the sibling file's fat arm
(tests/test_utxo_boost_abort.py: vanilla c1551c557 with a single
300-BTC wallet UTXO + force-close + stuck offered HTLC aborts in
~8-30ms at wallet.c:584 via anchorspend's commit_tx_boost; backtrace
banked at lightning-playground test-artifacts/
wallet-utxo-boost-abort-20260904/crash-fatarpm-backtrace.log).  This
arm revives the crashed daemon on the SAME lightning_dir - wallet and
channel DB intact - and measures whether the second incarnation
crashes the same way.

Depth-2 proof, not deadline exhaustion: waiting out "the deadline
window" is not practical inside one test; a FATAL SIGNAL 6 from the
SECOND process incarnation is the loop proof.  The log carries
exactly one 'FATAL SIGNAL 6' line per crash (see the banked backtrace
log), and lightningd APPENDS to its log file across incarnations
(lightningd/log.c:925, fopen(path, "a")), so "2 FATAL lines" == "2
crashes" == loop established at depth 2.  Depth 1 -> 2 implies
repetition; the deadline only bounds how far it runs.

Polarity: PASSES on vulnerable vanilla (crash #1 + crash #2).  On a
fixed build (skip/saturate instead of abort) it FAILS loudly at the
crash-#1 assert below - the may_fail/broken_log kwargs are teardown
expectations only, no softness in the body.  If vanilla instead
survives the restart, the crash-#2 wait times out (ValueError): that
DISPROVES the draft's loop claim - report it as the finding, don't
soften the test.

Restart idiom (pyln canonical, as in tests/test_misc.py
test_funding_reorg_remote_lags and tests/test_gossip.py):
LightningNode.restart() - stop() tolerates the already-dead daemon
(rpc.stop() OSError eaten at utils.py:1354, daemon.wait() returns the
crash rc immediately, may_fail=True set at get_node absorbs rc != 0
at utils.py:1365) - then start() relaunches the same LightningD opts:
same lightning_dir, same lightningd.sqlite3 + wallet, waiting
internally for 'Server started with public key' (utils.py:965) after
wait_for_port_released (utils.py:962).  If crash #2 lands before that
line, start()'s own wait times out (TimeoutError, an OSError
subclass) or the follow-up getinfo() hits the dead socket - a restart
that raises is the loop finding, not an error, so it is caught below
and the FATAL-count rail carries the verdict.  That rail deliberately
avoids a second bare wait_for_log: the monotonic logsearch_start
cursor (utils.py:505) would already have consumed past any
pre-'Server started' FATAL line, making it unmatchable (the pyln
chronological-log-order law).
"""

from fixtures import *  # noqa: F401,F403
from utils import wait_for  # noqa: F401

import pytest

from test_utxo_boost_abort import (  # noqa: F401
    FAT_DEPOSIT_SAT,
    _force_close_with_stuck_htlc,
    _node_with_single_utxo,
)


def _fatal_signal6_count(node):
    """Count 'FATAL SIGNAL 6' lines the daemon has EVER logged - one per
    crash (banked backtrace evidence).  Safe across restarts: lightningd
    appends to its log file (lightningd/log.c:925, fopen "a") and
    TailableProc never clears daemon.logs (utils.py:333), so this counts
    CRASHES, not incarnations - and never moves the logsearch_start
    cursor."""
    node.daemon.logs_catchup()
    return sum(1 for l in node.daemon.logs if 'FATAL SIGNAL 6' in l)


def test_utxo_boost_abort_restart_crash_loop_depth2(node_factory, bitcoind,
                                                    executor):
    """Revive the fat-arm crash and assert a SECOND FATAL SIGNAL 6: the
    draft's "crash loop until the deadline window passes" proven at
    depth 2 (module docstring carries the full claim, polarity, and the
    log/cursor mechanics)."""
    # Crash #1: setup identical to the fat arm - helpers imported
    # verbatim from test_utxo_boost_abort (single 300 BTC wallet UTXO,
    # anchors channel, stuck offered HTLC, dev_fail).
    l1, l2 = _node_with_single_utxo(
        node_factory, bitcoind, FAT_DEPOSIT_SAT,
        may_fail=True, broken_log=r'FATAL SIGNAL|backtrace|crash',
        feerates=(15000, 11000, 7500, 3750))
    _force_close_with_stuck_htlc(l1, l2, bitcoind, executor)

    # Crash #1 verdict rails - identical to the fat arm.
    l1.daemon.wait_for_log('FATAL SIGNAL 6')
    wait_for(lambda: l1.daemon.proc.poll() is not None)
    print("EXIT-CODE-1: {}".format(l1.daemon.proc.poll()))
    with pytest.raises(OSError):
        l1.rpc.getinfo()

    # Exactly one FATAL per crash pins the counter: the >= 2 below can
    # then only be satisfied by a crash from the NEW incarnation.
    assert _fatal_signal6_count(l1) == 1

    # Revive on the same lightning_dir (wallet + channel DB intact).
    # A TimeoutError/OSError out of start() means crash #2 beat the
    # 'Server started with public key' wait - the loop finding itself.
    try:
        l1.restart()
    except OSError:  # TimeoutError is an OSError subclass
        print("RESTART: daemon died before start() finished "
              "(crash #2 raced startup) - FATAL-count rail carries on")

    # Crash #2: the draft's loop, established at depth 2.  The helper
    # mined one block after the close broadcast, so the close tx is
    # 1-conf at restart; either re-entry - anchorspend's
    # commit_tx_boost or the HTLC-tx bump at onchain_control.c:1132 -
    # re-runs wallet_utxo_boost over the same fat UTXO and aborts here.
    wait_for(lambda: _fatal_signal6_count(l1) >= 2)
    wait_for(lambda: l1.daemon.proc.poll() is not None)
    print("EXIT-CODE-2: {}".format(l1.daemon.proc.poll()))
    fatal = _fatal_signal6_count(l1)
    # Diagnostic only: 1 'Server started' line = crash #2 raced
    # startup; 2 = it survived startup and died in onchaind handling.
    started = sum(1 for l in l1.daemon.logs
                  if 'Server started with public key' in l)
    print("RESTART-LOOP-DEPTH2: {} crashes, {} startups in one "
          "lightning_dir".format(fatal, started))
