from fixtures import *  # noqa: F401,F403
from pyln.client import RpcError
import pytest


def test_abort_channels_no_channel_ids(node_factory, bitcoind):
    """The bare abort_channels call (no channel_ids) must fail cleanly:
    it is a bad-parameter error, never a channel walk.  Pins the
    public-source behavior (crash regressions here are daemon-fatal)."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True)

    chan_id = l1.get_channel_id(l2)
    # The literal bare form: no channel_ids field at all.
    with pytest.raises(RpcError, match='Must specify a channel'):
        l1.rpc.call('abort_channels', {})

    # The channel is untouched by the refused call.
    assert only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])['state'] == 'CHANNELD_NORMAL'

    # And the documented form still works on a live channel is NOT
    # tested here (it force-closes); the refusal above is the contract.
