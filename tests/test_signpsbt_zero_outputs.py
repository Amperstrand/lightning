from fixtures import *  # noqa: F401,F403
from pyln.client import RpcError
import pytest


def test_signpsbt_zero_outputs_refused(node_factory, bitcoind):
    """utxopsbt satoshi=all discards excess_as_change and builds an
    output-less PSBT; signpsbt must refuse it with a typed error --
    signing a zero-output transaction would destroy the input value
    to fees -- not crash the daemon inside hsmd."""
    l1 = node_factory.get_node(options={"allow-deprecated-apis": True})
    l1.fundwallet(10**6)

    funds = l1.rpc.listfunds()["outputs"]
    utxos = ["{}:{}".format(u["txid"], u["output"]) for u in funds]
    r = l1.rpc.call("utxopsbt", {"satoshi": "all",
                                 "feerate": "253perkw",
                                 "startweight": 0,
                                 "excess_as_change": True,
                                 "utxos": utxos})
    # The discarded-flag shape: excess reported, no change output.
    assert "change_outnum" not in r
    assert "psbt" in r

    with pytest.raises(RpcError, match="no outputs"):
        l1.rpc.signpsbt(psbt=r["psbt"])

    # The node survived the refused signing; release the reservation.
    l1.rpc.getinfo()
    l1.rpc.unreserveinputs(psbt=r["psbt"])

    # Control: a partial amount with excess_as_change gets a change
    # output and signs cleanly.
    funds2 = [u for u in l1.rpc.listfunds()["outputs"]
              if not u["reserved"]]
    utxos2 = ["{}:{}".format(u["txid"], u["output"]) for u in funds2]
    r2 = l1.rpc.call("utxopsbt", {"satoshi": 500000,
                                   "feerate": "253perkw",
                                   "startweight": 0,
                                   "excess_as_change": True,
                                   "utxos": utxos2})
    assert "change_outnum" in r2
    signed = l1.rpc.signpsbt(psbt=r2["psbt"])
    assert signed["signed_psbt"]
