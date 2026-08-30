# Channel-restart discrimination probe (2026-08-30 decode follow-up):
# the plain no-channel restart PASSES (test_vls_plain_restart), while
# every splice test that restarts nodes wedges (restart-death class).
# This probe splits the discriminator: a node with a PLAIN channel
# (no splice) — if restart hangs, ANY channel at stop triggers the
# class; if it passes, the class is splice-state-specific.
from fixtures import *  # noqa: F401,F403
from utils import TEST_NETWORK, only_one


def test_vls_channel_restart(node_factory, bitcoind):
    l1, l2 = node_factory.line_graph(2)
    node_id = l1.rpc.getinfo()["id"]
    l1.restart()
    info = l1.rpc.getinfo()
    assert info["id"] == node_id, "identity survives restart"
    chan = only_one(l1.rpc.listpeerchannels()["channels"])
    assert chan["state"] == "CHANNELD_NORMAL", chan["state"]
