# Minimal restart isolation probe (2026-08-30 decode follow-up):
# does a PLAIN pyln restart of a VLS-backed node complete, or does it
# stall at the HsmdInit round-trip (the restart-death class signature:
# "hsmd: pid N, msgfd 74" then silence, requests reach the proxy)?
# Isolates the restart path from splice state entirely.
from fixtures import *  # noqa: F401,F403
from utils import TEST_NETWORK


def test_vls_plain_restart(node_factory):
    l1 = node_factory.get_node()
    node_id = l1.rpc.getinfo()["id"]
    l1.restart()
    info = l1.rpc.getinfo()
    assert info["id"] == node_id, "identity survives restart"
    assert info["network"] == TEST_NETWORK
