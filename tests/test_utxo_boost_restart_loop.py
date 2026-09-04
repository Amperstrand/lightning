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
crash-#1 death wait below - the may_fail/broken_log kwargs are teardown
expectations only, no softness in the body.  If vanilla instead
survives the restart, the crash-#2 wait times out (ValueError): that
DISPROVES the draft's loop claim - report it as the finding, don't
soften the test.

Why this arm carries NO ONCHAIN-transition wait (banked evidence:
lightning-playground test-artifacts/
wallet-utxo-boost-abort-20260904/vm-pull-loop): the sibling close
helper ends in a chain-observable wait gated on ONE instantaneous
poll(), and campaign A's run of this test observed the daemon
mid-death - FATAL SIGNAL 6 was already logged at the close broadcast
(+22 ms) but the crashdump teardown (backtrace print, crash.log dump
via bt_exit, kill(getpid()), kernel reap) had not finished - so the
gate took the chain-observable branch and burned the full TIMEOUT
(180 s) waiting for a log line the dead daemon could never emit (test
start 21:51:07, crash 21:51:14.538, fail 21:54:14 == crash + 180 s).
The loop verdict is FATAL-COUNT-based; chain state is an unrelated
observable that coupled this test to that death race.  The trigger
prefix is therefore INLINED here (minus the chain-observable wait)
instead of borrowed from the helper, and the crash-#1 death rail is
an explicit bounded wait_for(..., timeout=60): a daemon that neither
dies nor logged FATAL inside 60 s is a real anomaly and fails loudly
(ValueError).

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
    HTLC_MSAT,
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
    # Crash #1: setup identical to the fat arm (single 300 BTC wallet
    # UTXO, anchors channel) via _node_with_single_utxo, but the close
    # trigger is INLINED below rather than borrowed from the sibling
    # helper - the helper's chain-observable sync coupled this test to
    # the death race documented in the module docstring.
    l1, l2 = _node_with_single_utxo(
        node_factory, bitcoind, FAT_DEPOSIT_SAT,
        may_fail=True, broken_log=r'FATAL SIGNAL|backtrace|crash',
        feerates=(15000, 11000, 7500, 3750))

    # Trigger prefix, verbatim from the sibling helper MINUS its
    # chain-observable tail (module docstring): stuck offered HTLC,
    # then a unilateral close, mempool sync, one block.
    l2.rpc.dev_ignore_htlcs(id=l1.info['id'], ignore=True)
    executor.submit(l1.pay, l2, HTLC_MSAT)
    l2.daemon.wait_for_log(
        'htlc 0: SENT_ADD_ACK_COMMIT->RCVD_ADD_ACK_REVOCATION')

    # On a vulnerable build the abort can race the dev_fail reply
    # itself (the boost fires on the close broadcast); a dead socket
    # here is that same finding, not an error - the FATAL rails below
    # still run.
    try:
        l1.rpc.dev_fail(l2.info['id'])
    except OSError:
        pass
    # Sync the unilateral close tx into the mempool BEFORE mining
    # (proven sibling sequence); on a crashed daemon this raises
    # OSError immediately (dead RPC socket) and is swallowed.
    try:
        l1.wait_for_channel_onchain(l2.info['id'])
    except OSError:
        pass
    bitcoind.generate_block(1)

    # Crash #1 verdict rails.  Death FIRST and BOUNDED (60 s): the
    # banked vm-pull-loop failure proved a bare gate can observe a
    # crashed-but-unreaped daemon.  wait_for raises ValueError on
    # timeout - loud failure, by design: a daemon that neither died
    # nor logged FATAL is a real anomaly.
    wait_for(lambda: l1.daemon.proc.poll() is not None, timeout=60)
    print("EXIT-CODE-1: {}".format(l1.daemon.proc.poll()))
    l1.daemon.wait_for_log('FATAL SIGNAL 6')
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

    # Crash #2: the draft's loop, established at depth 2.  The trigger
    # prefix mined one block after the close broadcast, so the close tx is
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
