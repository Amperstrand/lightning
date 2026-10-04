import pytest
from fixtures import *  # noqa: F401,F403
from pyln.testing.utils import wait_for
from utils import *

# Startup reconciliation of spent-funding channels (channel_reconcile.c):
# a node restarting over a channel whose funding output was already
# spent must not reattach channeld to it and ask the signer for new
# commitments; the funding spend watch must take it onchain instead.
#
# These rails run on the stock HSM: they exercise the HOST behavior
# (never resurrect a dead channel), which is what a strict signer
# requires of us.

GATE_LOG = r'Funding output .* is spent according to bitcoind'


def logpos_after_last(node, marker='Server started'):
    """Position in the in-memory log buffer just after the last
    occurrence of marker (i.e. just after the most recent restart)."""
    node.daemon.logs_catchup()
    pos = None
    for i, line in enumerate(node.daemon.logs):
        if marker in line:
            pos = i + 1
    assert pos is not None, "no '{}' in logs".format(marker)
    return pos


def channel_of(node, peer):
    return only_one(node.rpc.listpeerchannels(peer.info['id'])['channels'])


def test_restart_unspent_funding_reattaches(node_factory, bitcoind):
    """Control rail: a healthy channel reattaches channeld on restart."""
    l1, l2 = node_factory.line_graph(2, opts={'may_reconnect': True})

    l1.restart()
    pos = logpos_after_last(l1)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l1.daemon.wait_for_log(r'-channeld-chan#1: ')
    assert channel_of(l1, l2)['state'] == 'CHANNELD_NORMAL'
    assert l1.daemon.is_in_log(GATE_LOG) is None


def spent_funding_restart(node_factory, bitcoind, l1_opts, confirm):
    """Common shape: extract l1's commitment, stop l1, broadcast it
    (optionally confirming it), restart l1, and prove the channel is
    never resurrected: the gate fires, no channeld attaches, the funding
    spend watch takes the channel onchain and it resolves."""
    l1, l2 = node_factory.line_graph(
        2, opts=[{**l1_opts, 'may_reconnect': True},
                 {'may_reconnect': True}])

    tx = l1.rpc.dev_sign_last_tx(l2.info['id'])['tx']
    l1.stop()
    bitcoind.rpc.sendrawtransaction(tx)
    txid = bitcoind.rpc.decoderawtransaction(tx, True)['txid']
    if confirm:
        bitcoind.generate_block(5, wait_for_mempool=txid)

    l1.restart()

    # The gate fired at startup, before any subdaemon could attach.
    # (It logs before "Server started", so search the whole buffer.)
    wait_for(lambda: l1.daemon.is_in_log(GATE_LOG) is not None)
    pos = logpos_after_last(l1)

    # In the mempool case the channel defers: prove the deferral
    # survives an actual peer connection attempt (the gate errors the
    # reestablish and disconnects, no channeld ever spawns).  In the
    # confirmed case the rescan resolves the channel within moments, so
    # there is nothing left to refuse.
    if not confirm:
        l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
        l1.daemon.wait_for_log(r'Peer has (re)?connected')
        l1.daemon.logs_catchup()
    assert l1.daemon.is_in_log(r'-channeld-chan#1: ', start=pos) is None

    # The existing watch machinery does the transition once the spend
    # confirms (possibly via AWAITING_UNILATERAL) and onchaind takes over.
    bitcoind.generate_block(1)
    wait_for(lambda: l1.daemon.is_in_log(r'-onchaind-chan#1: ') is not None)
    wait_for(lambda: channel_of(l1, l2)['state'] == 'ONCHAIN')

    # And the channel fully resolves and is forgotten onchain (the
    # forgetting window only starts once the outputs resolve).
    bitcoind.generate_block(100)
    wait_for(lambda: l1.daemon.is_in_log(
        r'-onchaind-chan#1: billboard: All outputs resolved') is not None)
    bitcoind.generate_block(101)
    wait_for(lambda: l1.rpc.listpeerchannels()['channels'] == [])
    wait_for(lambda: l2.rpc.listpeerchannels()['channels'] == [])


@pytest.mark.openchannel('v1')
def test_restart_spent_funding_confirmed(node_factory, bitcoind):
    """Spent-and-confirmed funding: no channeld attach, straight to
    onchaind via the funding spend watch (the startup rescan)."""
    spent_funding_restart(node_factory, bitcoind, {'may_fail': True},
                          confirm=True)


@pytest.mark.openchannel('v1')
def test_restart_spent_funding_mempool(node_factory, bitcoind):
    """Spend in mempool only: RAIL DECISION - the channel defers attach
    (AWAITING_UNILATERAL-style: no new commitments are asked of the
    signer) until the spend confirms, then the watch takes it onchain."""
    spent_funding_restart(node_factory, bitcoind, {'may_fail': True},
                          confirm=False)


@pytest.mark.openchannel('v2')
def test_restart_spent_funding_confirmed_v2(node_factory, bitcoind):
    """Same confirmed-spend rail for a v2 (dual-funded) channel: the
    gate covers both openprotocol families' live states."""
    spent_funding_restart(node_factory, bitcoind,
                          {'may_fail': True,
                           'experimental-dual-fund': None},
                          confirm=True)
