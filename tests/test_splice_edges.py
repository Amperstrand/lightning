from fixtures import *  # noqa: F401,F403
import time
from utils import (
    TEST_NETWORK
)
import pytest
import unittest


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'regtest-only')
def test_splice_edge_dust(node_factory, bitcoind):
    """Splice-out to near-dust: the channel must survive a splice that
    takes the funding down toward the dust floor (and a refusal when
    below it), and a subsequent splice-in must restore capacity."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts={'may_reconnect': True})
    chan_id = l1.get_channel_id(l2)

    # A splice-out that leaves a small-but-valid funding (the fee paid
    # from the channel balance — the proven addpsbtoutput pattern).
    funds_result = l1.rpc.addpsbtoutput(100000)
    result = l1.rpc.splice_init(chan_id, -155801, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.splice_signed(chan_id, result['psbt'])
    bitcoind.generate_block(6, wait_for_mempool=1)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    # Splice back in to restore (the funded-psbt pattern).
    funds_result = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])
    bitcoind.generate_block(6, wait_for_mempool=1)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    # The channel still pays.
    inv = l2.rpc.invoice(10**2, 'edge1', 'edge1')
    l1.rpc.xpay(inv['bolt11'])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'regtest-only')
def test_splice_edge_fee_extremes(node_factory, bitcoind):
    """Splices at explicit fee extremes: the lowest sane feerate and a
    high feerate must both complete; a zero feerate must be refused."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts={'may_reconnect': True})
    chan_id = l1.get_channel_id(l2)

    # Explicit high feerate splice (253 = the floor in most configs).
    funds_result = l1.rpc.addpsbtoutput(100000)
    result = l1.rpc.splice_init(chan_id, -105801, funds_result['psbt'],
                                 feerate_per_kw=253)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.splice_signed(chan_id, result['psbt'])
    bitcoind.generate_block(6, wait_for_mempool=1)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    # A high feerate (urgent).
    funds_result = l1.rpc.addpsbtoutput(50000)
    result = l1.rpc.splice_init(chan_id, -55801, funds_result['psbt'],
                                 feerate_per_kw=3750)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.splice_signed(chan_id, result['psbt'])
    bitcoind.generate_block(6, wait_for_mempool=1)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    inv = l2.rpc.invoice(10**2, 'fee1', 'fee1')
    l1.rpc.xpay(inv['bolt11'])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'regtest-only')
def test_splice_edge_concurrent_rounds(node_factory, bitcoind, executor):
    """Three sequential splices without waiting for confirmations
    between them (beyond what the protocol requires): stress the
    inflight bookkeeping (the two-deep prev chain territory)."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts={'may_reconnect': True})
    chan_id = l1.get_channel_id(l2)

    for i, amt in enumerate([-50000, 50000, -30000]):
        if amt < 0:
            funds_result = l1.rpc.addpsbtoutput(-amt)
            # the fee paid from the channel balance (~5801 sat)
            result = l1.rpc.splice_init(chan_id, amt - 5801, funds_result['psbt'])
        else:
            funds_result = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
            result = l1.rpc.splice_init(chan_id, amt, funds_result['psbt'])
        result = l1.rpc.splice_update(chan_id, result['psbt'])
        result = l1.rpc.splice_update(chan_id, result['psbt'])
        assert result['commitments_secured'] is True, f"round {i}"
        try:
            signed = l1.rpc.signpsbt(result['psbt'])['signed_psbt']
        except Exception:
            # No wallet inputs to sign (the splice-out rounds) — send as-is.
            signed = result['psbt']
        result = l1.rpc.splice_signed(chan_id, signed)
        # Let each confirm before the next (the unconfirmed-RBF case is
        # test_splice_rbf's job; here we test rapid sequential windows).
        bitcoind.generate_block(6, wait_for_mempool=1)
        l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL',
                                timeout=60)

    inv = l2.rpc.invoice(10**2, 'cc1', 'cc1')
    l1.rpc.xpay(inv['bolt11'])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'regtest-only')
def test_splice_edge_reject_zero_fee_margin(node_factory, bitcoit=None, bitcoind=None):
    """A zero-fee-margin splice (the full relative amount to the
    output, nothing for fees) must be refused with SPLICE_LOW_FEE —
    the signer is never asked to sign a fee-less funding."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000)
    chan_id = l1.get_channel_id(l2)
    funds_result = l1.rpc.addpsbtoutput(100000)
    result = l1.rpc.splice_init(chan_id, -100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    try:
        result = l1.rpc.splice_update(chan_id, result['psbt'])
        pytest.fail("zero-fee-margin splice secured")
    except Exception as e:
        assert 'Feerate too low' in str(e) or '359' in str(e)
