"""E2b restart variant — the #257 fix must recover even when the
conflicting spend confirms while the node is DOWN: on restart the
topology catch-up should fire the inflight-input watcher and abort the
dead splice instead of re-wedging.
"""
from fixtures import *  # noqa: F401,F403
from utils import only_one, wait_for
import time


def test_e2b_restart_during_conflict(node_factory, bitcoind):
    nopts = {"disable-plugin": ["cln-currencyrate"]}
    l1, l2 = node_factory.get_nodes(2, opts=[nopts, nopts])
    l2.rpc.connect(l1.info["id"], "localhost", l1.port)
    l2.fundchannel(l1, 10**6)
    l1.daemon.wait_for_log(" to CHANNELD_NORMAL")
    bitcoind.generate_block(8)
    chan_id = only_one(
        l2.rpc.listpeerchannels(l1.info["id"])["channels"])["channel_id"]

    # wallet shaping: leave ~115k spendable
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

    # splice in 100k from the wallet, secure, sign, broadcast
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
    signed = l2.rpc.signpsbt(psbt=psbt)
    l2.rpc.splice_signed(chan_id, signed["signed_psbt"])

    inflight = None
    for _ in range(60):
        ch = only_one(l2.rpc.listpeerchannels(l1.info["id"])["channels"])
        inflights = ch.get("inflight") or []
        if inflights:
            inflight = inflights[-1]
            break
        try:
            bitcoind.generate_block(1)
        except Exception:
            pass
        time.sleep(1)
    assert inflight, "no inflight registered"

    # double-spend the splice input with a fat-fee conflict
    r = l2.rpc.unreserveinputs(psbt=psbt)
    addr = l2.rpc.newaddr("bech32")["bech32"]
    big = max(utxos_now, key=sat)
    out_amt = sat(big) - 76_000
    conflict = l2.rpc.txprepare(outputs=[{addr: "{}sat".format(out_amt)}],
                                feerate="180000perkw",
                                utxos=["{}:{}".format(big["txid"],
                                                      big["output"])])
    l2.rpc.txsend(conflict["txid"])

    # THE VARIANT: the splice initiator is DOWN while the conflict
    # confirms; recovery must happen on restart via the topology
    # catch-up, not from a live watcher.
    l2.stop()
    bitcoind.generate_block(1, wait_for_mempool=conflict["txid"])
    bitcoind.generate_block(2)
    l2.start()

    # The watcher should fire during catch-up and resolve the channel
    # out of the splice wait; allow up to 3 minutes.
    def resolved():
        ch = only_one(l2.rpc.listpeerchannels(l1.info["id"])["channels"])
        return ch["state"] != "CHANNELD_AWAITING_SPLICE"

    wait_for(resolved, timeout=180)
    ch = only_one(l2.rpc.listpeerchannels(l1.info["id"])["channels"])
    print("E2B_RESTART_FINAL state={}".format(ch["state"]))
    assert ch["state"] in ("AWAITING_UNILATERAL", "ONCHAIN",
                           "CHANNELD_NORMAL"), ch["state"]
    assert ch["funding_txid"] == only_one(
        l1.rpc.listpeerchannels(l2.info["id"])["channels"])["funding_txid"]
