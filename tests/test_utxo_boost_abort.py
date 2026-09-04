"""Audit item D probe: wallet_utxo_boost / calc_feerate u32-feerate abort.

Hypothesis (frozen BEFORE this run, see lightning-playground
test-artifacts/wallet-utxo-boost-abort-20260904/hypothesis.md and
docs/cln-assert-audit-2026-09-02.md item D):

    wallet/wallet.c:584 abort() fires inside calc_feerate() when a boost
    candidate UTXO makes fee*1000/weight exceed u32 max:
        abort  <=>  (excess0 + A - required) * 1000 >= 2**32 * (W0 + w_in)
    Input weights (bitcoin/tx.c, common/utxo.c): P2TR = 230 wu,
    P2WPKH = 271 wu.  Single-candidate boundaries derived there:
    HTLC-tx bump path (onchain_control.c:1132): A >= ~30.5-34.2 BTC;
    anchor spend path (anchorspend.c:310):      A >= ~49.7-55.3 BTC.

    Trigger is NOT deadline-gated: consider_onchain_htlc_tx_rebroadcast
    runs at HTLC-tx SIGN time ("Immediately consider RBF",
    onchain_control.c:1603) and commit_tx_boost fires when the unilateral
    close tx is seen in mempool - both within seconds of a force close
    carrying a pending offered HTLC.  Nothing between the candidate and
    the abort caps the amount (the uneconomic-skip at wallet.c:644 sits
    strictly AFTER the calc_feerate call at :640).

Polarity: arm 1 PASSES on vulnerable vanilla c1551c557 (it asserts the
daemon dies) - that pass IS the finding.  A fix (skip/saturate instead of
abort, hypothesis section 6) flips arm 1 to failure and must keep arms
2/3 green; run fix-side campaigns with the inverted expectation.  Arms
2 (10 BTC, below boundary) and 3 (1 BTC control) pass on both and
bracket the boundary: same scenario, only the deposit value differs.

Scenario per arm: a wallet holding EXACTLY ONE confirmed UTXO (the
deposit; the channel open must spend it, leaving a single fat change),
one channel, one offered HTLC the peer commits but never resolves
(dev_ignore_htlcs, cribbed from test_closing.py::
test_onchain_different_fees), then dev_fail -> unilateral close.
"""

from fixtures import *  # noqa: F401,F403
from utils import only_one, sync_blockheight, wait_for  # noqa: F401

import pytest

# The single experimental variable: the deposit that becomes the only
# spendable wallet UTXO (as fundchannel change) when the close fires.
# 300 BTC is >= 5.4x above the worst derived boundary (~55.3 BTC, anchor
# path); 10 BTC implies feerate 10**9*1000/711 = 1,406,469,776 perkw,
# ~3x below 2**32 (site reached, must NOT abort); 1 BTC is an ordinary
# wallet.
FAT_DEPOSIT_SAT = 300 * 10**8
BELOW_DEPOSIT_SAT = 10 * 10**8
CONTROL_DEPOSIT_SAT = 1 * 10**8

CHANNEL_SAT = 10**7
HTLC_MSAT = 10**8

U32_MAX = 2**32 - 1


def _node_with_single_utxo(node_factory, bitcoind, deposit_sat,
                           may_fail=False, broken_log=None, opts=None,
                           feerates=(15000, 11000, 7500, 3750)):
    """Return (l1, l2) where l1's wallet holds EXACTLY ONE confirmed
    spendable UTXO worth ~deposit_sat, with a confirmed channel to l2.

    Uses rpc.fundchannel directly: the Node.fundchannel helper pre-funds
    an extra small UTXO (pyln utils.py: send_and_mine_block(addr,
    amount + 1000000)), which would leave a second candidate that can
    satisfy feerate_target on its own and early-return wallet_utxo_boost
    before the fat UTXO is ever examined - a coin-flip, not a verdict.
    With a single deposit UTXO the loop provably examines it: initial
    feerate is 0 and feerate_target >= FEERATE_FLOOR (253).

    may_fail/broken_log are get_node kwargs (teardown expectations), NOT
    lightningd options: get_node(options=...) passes the dict verbatim
    as CLI flags, so pyln-internal keys here would die as
    "--may_fail=True: unknown option".
    """
    l1 = node_factory.get_node(options=opts if opts else {},
                               feerates=feerates,
                               may_fail=may_fail, broken_log=broken_log)
    l2 = node_factory.get_node()

    addr = l1.rpc.newaddr('bech32')['bech32']
    # The shared fixture wallet starts with almost no mature balance and
    # regtest coinbases need 100 confs to spend: mine a batch so ~12
    # coinbases (600 BTC) mature, covering the 300 BTC fat arm.
    bitcoind.generate_block(112)
    sync_blockheight(bitcoind, [l1, l2])
    bitcoind.send_and_mine_block(addr, deposit_sat)
    sync_blockheight(bitcoind, [l1, l2])
    wait_for(lambda: l1.rpc.listfunds()['outputs'] != [])
    dep = only_one(l1.rpc.listfunds()['outputs'])
    assert dep['status'] == 'confirmed'

    l1.connect(l2)
    res = l1.rpc.fundchannel(l2.info['id'], CHANNEL_SAT)
    bitcoind.generate_block(1, wait_for_mempool=res['txid'])
    sync_blockheight(bitcoind, [l1, l2])
    wait_for(lambda: l1.channel_state(l2) == 'CHANNELD_NORMAL')

    # Evidence: channel type must be anchors/even (zero-fee HTLC txs make
    # output_sats_required 0 for the bump path, and
    # consider_onchain_htlc_tx_rebroadcast requires anchors at all).
    chan = only_one(l1.rpc.listpeerchannels()['channels'])
    assert 'anchors/even' in chan['channel_type']['names'], chan['channel_type']

    # The only spendable wallet output is now the (fat) change: exactly
    # the candidate set wallet_utxo_boost will see.
    change = only_one(l1.rpc.listfunds()['outputs'])
    assert change['status'] == 'confirmed'
    print("SINGLE-UTXO-WALLET: {} amt={}".format(
        change['address'], change['amount_msat']))
    return l1, l2


def _force_close_with_stuck_htlc(l1, l2, bitcoind, executor):
    """Offered HTLC committed on both sides but never fulfilled, then a
    unilateral close - the state that makes lightningd ask its wallet to
    boost the HTLC-timeout tx and the commitment anchor."""
    l2.rpc.dev_ignore_htlcs(id=l1.info['id'], ignore=True)
    executor.submit(l1.pay, l2, HTLC_MSAT)
    l2.daemon.wait_for_log(
        'htlc 0: SENT_ADD_ACK_COMMIT->RCVD_ADD_ACK_REVOCATION')

    # On a vulnerable build the abort can race the dev_fail reply itself
    # (the boost fires on the close broadcast); a dead socket here is
    # that same finding, not an error - the FATAL wait below still runs.
    try:
        l1.rpc.dev_fail(l2.info['id'])
    except OSError:
        pass
    # Proven sibling sequence (test_onchain_different_fees): sync the
    # unilateral close tx into the mempool BEFORE mining.  A bare
    # generate_block(1) races the close broadcast - the mined block
    # misses the tx, the channel never leaves AWAITING_UNILATERAL and
    # ' to ONCHAIN' never appears (run4 evidence: the commitment tx was
    # re-broadcast AFTER the generated block was added).
    try:
        l1.wait_for_channel_onchain(l2.info['id'])
    except OSError:
        pass
    bitcoind.generate_block(1)
    # Only a live daemon can log the ONCHAIN transition; on a vulnerable
    # build the abort (the finding) killed l1 at the close broadcast and
    # the test body's FATAL wait is the verdict.
    if l1.daemon.proc.poll() is None:
        l1.daemon.wait_for_log(' to ONCHAIN')


def test_utxo_boost_abort_fat_utxo_fails_loudly(node_factory, bitcoind,
                                                executor):
    """VANILLA c1551c557 prediction (hypothesis section 4): a single
    ~300 BTC wallet UTXO plus a force-closed channel with a pending
    offered HTLC aborts lightningd at wallet/wallet.c:584 (SIGABRT from
    calc_feerate -> amount_feerate -> assign_overflow_u32), reached via
    wallet_utxo_boost from onchain_control.c:1132 (HTLC-tx bump, sign
    time) and/or anchorspend.c:310 (anchor spend).  This test PASSES on
    a vulnerable build - the pass is the finding - and fails on a build
    that skips/saturates instead of aborting.
    """
    l1, l2 = _node_with_single_utxo(
        node_factory, bitcoind, FAT_DEPOSIT_SAT,
        may_fail=True, broken_log=r'FATAL SIGNAL|backtrace|crash',
        feerates=(15000, 11000, 7500, 3750))
    _force_close_with_stuck_htlc(l1, l2, bitcoind, executor)

    # The abort site: FATAL SIGNAL 6 (SIGABRT), printed by the crash
    # handler with the backtrace through wallet_utxo_boost.
    l1.daemon.wait_for_log('FATAL SIGNAL 6')

    # Daemon really died (the log line alone could outlive a restart):
    # SIGABRT kills the process with a negative poll() rc.
    wait_for(lambda: l1.daemon.proc.poll() is not None)
    print("EXIT-CODE: {}".format(l1.daemon.proc.poll()))

    # ... and the RPC surface is gone (connection refused family).
    with pytest.raises(OSError):
        l1.rpc.getinfo()


def test_utxo_boost_below_boundary_survives(node_factory, bitcoind,
                                            executor):
    """Boundary bracket, below side: a single 10 BTC UTXO implies
    feerate 1,406,469,760 perkw at the bump weight - the abort site
    EXECUTES (wallet_utxo_boost must log) but the quotient fits u32 and
    no abort may happen, on any build.  Together with the 300 BTC arm
    this brackets the real boundary (~30.5-55.3 BTC) without trusting
    the W0 estimate.
    """
    l1, l2 = _node_with_single_utxo(
        node_factory, bitcoind, BELOW_DEPOSIT_SAT,
        feerates=(15000, 11000, 7500, 3750))
    _force_close_with_stuck_htlc(l1, l2, bitcoind, executor)

    # wallet_utxo_boost ran and logged (either "got N UTXOs" early
    # return or "fell short"): the abort site was reached with a
    # near-boundary candidate and correctly did not abort.
    l1.daemon.wait_for_log('wallet_utxo_boost:')

    # Daemon survived the whole deadline-bump consideration.
    assert l1.rpc.getinfo()['id']
    assert not l1.daemon.is_in_log('FATAL SIGNAL')


def test_utxo_boost_control_normal_wallet_survives(node_factory, bitcoind,
                                                   executor):
    """Control arm: identical scenario with an ordinary 1 BTC wallet
    (implied feerate ~1.4e8 perkw).  Proves the close/boost scenario is
    itself benign - only the fat UTXO kills - i.e. the crash in arm 1 is
    value-driven, not environmental (the #9452-class discriminator).
    """
    l1, l2 = _node_with_single_utxo(
        node_factory, bitcoind, CONTROL_DEPOSIT_SAT,
        feerates=(15000, 11000, 7500, 3750))
    _force_close_with_stuck_htlc(l1, l2, bitcoind, executor)

    l1.daemon.wait_for_log('wallet_utxo_boost:')
    assert l1.rpc.getinfo()['id']
    assert not l1.daemon.is_in_log('FATAL SIGNAL')
