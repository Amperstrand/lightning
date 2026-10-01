from fixtures import *  # noqa: F401,F403
from utils import wait_for, only_one
from pyln.client import RpcError
import pytest


def test_abort_channels_no_channel_ids(node_factory, bitcoind):
    """The bare abort_channels/stfu_channels call (no channel_ids) must
    fail cleanly: never walk a NULL array token.  Pins the fixed
    behavior (regressions here are daemon-fatal)."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True)

    # The literal bare form: no channel_ids field at all.
    with pytest.raises(RpcError, match='Must specify a channel'):
        l1.rpc.call('abort_channels', {})
    with pytest.raises(RpcError, match='Must specify a channel'):
        l1.rpc.call('stfu_channels', {})

    # The channels are untouched by the refused calls.
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL')
    wait_for(lambda: only_one(l2.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL')
