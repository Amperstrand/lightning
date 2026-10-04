"""A restart during the splice-confirmation window must not wedge the channel.

Depth notifications that fire while the channel has no owner (blocks
processed during lightningd's startup catch-up, before channeld
attaches) used to be silently dropped, and the attach site never
replayed the real inflight depth: after a restart inside the
splice-confirmation window the channel parked in CHANNELD_AWAITING_SPLICE
until an arbitrary NEXT block re-fired the depth watch - on regtest,
forever.

With the depth replay at attach, the freshly attached channeld learns
the confirmed depth immediately: the splice locks with no further
blocks.
"""
from fixtures import *  # noqa: F401,F403
from utils import wait_for, only_one
import time


def test_splice_restart_before_depth_processing(node_factory, bitcoind):
    nopts = {"disable-plugin": ["cln-currencyrate"],
             "may_reconnect": True,
             "dev-no-reconnect": None}
    l1, l2 = node_factory.get_nodes(2, opts=[nopts, nopts])
    l2.rpc.connect(l1.info["id"], "localhost", l1.port)
    l2.fundchannel(l1, 10**6)
    l1.daemon.wait_for_log(" to CHANNELD_NORMAL")
    bitcoind.generate_block(8)
    chan_id = only_one(
        l2.rpc.listpeerchannels(l1.info["id"])["channels"])["channel_id"]

    # Wallet shaping: one spendable on-chain utxo for the splice input.
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
        if len(utxos_now) == 2 and \
                135_000 <= sum(sat(u) for u in utxos_now) <= 160_000:
            break
        time.sleep(1)
    assert len(utxos_now) == 2, "wallet shaping failed"

    # Splice in from the wallet, sign, broadcast - but do NOT mine.
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
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 1)

    # THE WINDOW: disconnect, stop the accepter, confirm the splice
    # while it is down (its startup catch-up will drop the depth
    # notifications), restart and reconnect - and mine NOTHING more.
    l2.rpc.disconnect(l1.info["id"], force=True)
    l1.stop()
    bitcoind.generate_block(6, wait_for_mempool=1)
    l1.start()
    l2.rpc.connect(l1.info["id"], "localhost", l1.port)

    # The replayed depth must drive the splice to lock with no further
    # blocks.  Unpatched: both park in CHANNELD_AWAITING_SPLICE
    # forever.
    wait_for(lambda: only_one(
        l1.rpc.listpeerchannels(l2.info["id"])["channels"])["state"]
        == "CHANNELD_NORMAL", timeout=120)
    wait_for(lambda: only_one(
        l2.rpc.listpeerchannels(l1.info["id"])["channels"])["state"]
        == "CHANNELD_NORMAL", timeout=120)

    # Locked on the new funding (splice inflights cleared), channel
    # still routes.
    ch = only_one(l1.rpc.listpeerchannels(l2.info["id"])["channels"])
    assert not ch.get("inflight"), ch.get("inflight")
    inv = l1.rpc.invoice(10**7, "postsplice", "post")
    l2.rpc.pay(inv["bolt11"])
    wait_for(lambda: only_one(
        l1.rpc.listinvoices("postsplice")["invoices"])["status"] == "paid")
    for n in (l1, l2):
        assert not n.daemon.is_in_log("FATAL")
