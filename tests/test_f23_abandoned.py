"""F23 / LP-CLN-2026-003: abandoned-after-secured splice.

The initiator takes a splice through commitments_secured (channeld
signatures exchanged, a wallet input added) but never provides the
wallet signatures and never broadcasts the splice tx.  A connection
churn then drives both sides towards tx_abort -> protocol error ->
AWAITING_UNILATERAL.

The splice tx can never confirm (its wallet inputs can never be
signed), so the splice-series commitments can never confirm either:
publishing them is an unconfirmable retry loop.  The channel must
instead resolve on the ORIGINAL funding (whose commitment is valid),
and the funds must be recoverable.
"""
from fixtures import *  # noqa: F401,F403
from utils import wait_for, only_one
import time


def test_f23_abandoned_after_secured(node_factory, bitcoind):
    nopts = {"disable-plugin": ["cln-currencyrate"]}
    l1, l2 = node_factory.get_nodes(2, opts=[nopts, nopts])
    l2.rpc.connect(l1.info["id"], "localhost", l1.port)
    l2.fundchannel(l1, 10**6)
    l1.daemon.wait_for_log(" to CHANNELD_NORMAL")
    bitcoind.generate_block(8)
    chan_id = only_one(
        l2.rpc.listpeerchannels(l1.info["id"])["channels"])["channel_id"]

    # Wallet shaping so the splice adds an unsigned wallet input.
    def sat(u):
        ms = u["amount_msat"]
        return int(ms[:-4]) // 1000 if isinstance(ms, str) \
            and ms.endswith("msat") else int(ms) // 1000
    bitcoind.generate_block(6, wait_for_mempool=None)
    pre = [u for u in l2.rpc.listfunds()["outputs"]
           if u["status"] == "confirmed"]
    pre_total = sum(sat(u) for u in pre)
    sweep_addr = bitcoind.rpc.getnewaddress()
    spare_addr = l2.rpc.newaddr("bech32")["bech32"]
    amt = pre_total - 30_000 - 115_738 - 5_000
    t = l2.rpc.txprepare(outputs=[{sweep_addr: "{}sat".format(amt)},
                                  {spare_addr: "30000sat"}],
                         feerate="7505perkw",
                         utxos=["{}:{}".format(u["txid"], u["output"])
                                for u in pre])
    l2.rpc.txsend(t["txid"])
    bitcoind.generate_block(1, wait_for_mempool=t["txid"])
    utxos_now = []
    for _ in range(6):
        bitcoind.generate_block(1)
        utxos_now = [u for u in l2.rpc.listfunds()["outputs"]
                     if u["status"] == "confirmed" and not u["reserved"]]
        if len(utxos_now) == 2 and 135_000 <= sum(sat(u) for u in utxos_now) <= 160_000:
            break
        time.sleep(1)
    assert len(utxos_now) == 2, "wallet shaping failed"

    # Splice to commitments_secured, then ABANDON (no wallet sigs, no
    # broadcast).
    psbt = l2.rpc.splice_init(chan_id, 100000)["psbt"]
    r = l2.rpc.addpsbtinput(satoshi=100000, initialpsbt=psbt,
                            min_feerate=253,
                            add_initiator_serial_ids=True,
                            mark_our_inputs=True)
    psbt = r["psbt"]
    commitments = None
    for i in range(8):
        r = l2.rpc.splice_update(chan_id, psbt)
        psbt = r["psbt"]
        commitments = r.get("commitments_secured")
        if commitments:
            break
    assert commitments, "splice_update never secured commitments"

    # Connection churn: force the disconnect (the quiescent channel
    # may reject the payment politely without killing the connection),
    # then reconnect to drive the reestablish sequence.
    inv = l1.rpc.invoice(10**8, "churn", "churn")
    try:
        l2.rpc.pay(inv["bolt11"], timeout=5)
    except Exception:
        pass
    time.sleep(2)
    l2.rpc.disconnect(l1.info["id"], force=True)
    time.sleep(3)
    l2.rpc.connect(l1.info["id"], "localhost", l1.port)
    time.sleep(10)

    def state(n, other):
        return only_one(
            n.rpc.listpeerchannels(other)["channels"])["state"]

    print("F23_T0 l2={} l1={}".format(state(l2, l1.info["id"]),
                                      state(l1, l2.info["id"])))
    print("F23_RESUME_MISSING_SIGS={}".format(
        l2.daemon.is_in_log("user sig(s) are missing")))
    print("F23_RETRY_LOOP={}".format(
        l2.daemon.is_in_log("bad-txns-inputs-missingorspent")))

    # THE CONTRACT: the accepter must not wedge forever.  The peer
    # (initiator) dropped the splice; on reconnect the accepter drops
    # its dead inflight and the channel returns to NORMAL (or, worst
    # case, resolves on-chain on the ORIGINAL funding) -- either way
    # it must leave CHANNELD_AWAITING_SPLICE and stay usable.
    def resolved():
        s1 = state(l1, l2.info["id"])
        return s1 != "CHANNELD_AWAITING_SPLICE"
    wait_for(resolved, timeout=180)
    print("F23_FINAL l2={} l1={}".format(state(l2, l1.info["id"]),
                                         state(l1, l2.info["id"])))
    for n in (l1, l2):
        assert not n.daemon.is_in_log("FATAL")

    # If the channel survived, it must still route.
    if state(l1, l2.info["id"]) == "CHANNELD_NORMAL" and \
       state(l2, l1.info["id"]) == "CHANNELD_NORMAL":
        inv = l1.rpc.invoice(10**7, "postrecovery", "post")
        l2.rpc.pay(inv["bolt11"])
        wait_for(lambda: only_one(
            l1.rpc.listinvoices("postrecovery")["invoices"])["status"] == "paid")
        print("F23_CHANNEL_SURVIVED routes=yes")
