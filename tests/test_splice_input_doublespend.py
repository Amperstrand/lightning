"""A confirmed double-spend of a splice input must not wedge the channel.

The splice initiator adds a wallet input to the splice, takes the
negotiation through commitments_secured, signs and broadcasts the
splice tx, then double-spends its own wallet input with a fat-fee
conflicting transaction.  When the conflict confirms, the splice tx
can never confirm: an armed-but-dead inflight remains registered and
(sans fix) the channel sticks in CHANNELD_AWAITING_SPLICE forever,
surviving reconnects and restarts, until an operator intervenes.

With the inflight-input watcher, the channel resolves automatically:
channeld aborts the dead splice; an already-signed splice force-closes,
resolving the channel on-chain on the original funding.
"""
from fixtures import *  # noqa: F401,F403
from utils import only_one, wait_for
import time


def test_splice_input_doublespend(node_factory, bitcoind):
    nopts = {"disable-plugin": ["cln-currencyrate"],
             "may_reconnect": True}
    l1, l2 = node_factory.get_nodes(2, opts=[nopts, nopts])
    l2.rpc.connect(l1.info["id"], "localhost", l1.port)
    l2.fundchannel(l1, 10**6)
    l1.daemon.wait_for_log(" to CHANNELD_NORMAL")
    bitcoind.generate_block(8)
    chan_id = only_one(
        l2.rpc.listpeerchannels(l1.info["id"])["channels"])["channel_id"]
    orig_funding = only_one(
        l2.rpc.listpeerchannels(l1.info["id"])["channels"])["funding_txid"]

    # Wallet shaping: leave one ~115k-sat utxo spendable on-chain so
    # the splice adds a real wallet input (which the initiator can
    # later double-spend).
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
    assert amt > 546, "not enough to sweep: %d" % amt
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
    assert len(utxos_now) == 2, \
        "wallet shaping failed: %s" % [(u['txid'][:8], sat(u))
                                       for u in utxos_now]

    # Splice in 100k from the wallet: base psbt + explicit wallet
    # funding via addpsbtinput (initiator serial ids + mark_our_inputs).
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

    # Wait for the splice tx broadcast to register the inflight.
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
    splice_txid = inflight["funding_txid"]

    # Double-spend the wallet input: unreserve it, then out-fee the
    # splice with a conflict (RBF rule 3).
    r = l2.rpc.unreserveinputs(psbt=psbt)
    addr = l2.rpc.newaddr("bech32")["bech32"]
    big = max(utxos_now, key=sat)
    out_amt = sat(big) - 76_000
    conflict = l2.rpc.txprepare(outputs=[{addr: "{}sat".format(out_amt)}],
                                feerate="180000perkw",
                                utxos=["{}:{}".format(big["txid"],
                                                      big["output"])])
    l2.rpc.txsend(conflict["txid"])

    bitcoind.generate_block(1, wait_for_mempool=conflict["txid"])
    bitcoind.generate_block(10)

    # The channel must leave CHANNELD_AWAITING_SPLICE on its own; the
    # already-signed splice force-closes, resolving on the ORIGINAL
    # funding.  Unpatched: stuck forever.
    def resolved():
        ch = only_one(l2.rpc.listpeerchannels(l1.info["id"])["channels"])
        return ch["state"] != "CHANNELD_AWAITING_SPLICE"

    wait_for(resolved, timeout=180)

    for n, tag in ((l1, "accepter"), (l2, "attacker")):
        peer = l2.info["id"] if n is l1 else l1.info["id"]
        ch = only_one(n.rpc.listpeerchannels(peer)["channels"])
        assert ch["funding_txid"] == orig_funding or \
            "ONCHAIN" in ch["state"] or ch["state"] == "CHANNELD_NORMAL", \
            (tag, ch["state"], ch["funding_txid"])
    for n in (l1, l2):
        assert not n.daemon.is_in_log("FATAL")
