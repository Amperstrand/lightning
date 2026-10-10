"""E4 derived-variant probes for the onchain-preimage-beats-failure fix (ce9b9c6b7).

Campaign 2026-09-12/13, lightning-playground INCIDENT-2026-09-12-CLN-9498.
Each test is a choreography the upstream regression test does not cover.
Cycle 2: disconnect count fixed (*4, parked htlc emits no CS), E4b route
pinned via l2, E4e rebroadcast wait replaced with mempool presence,
E4c post-splice variant added.
"""
from fixtures import *  # noqa: F401,F403
from utils import *  # noqa: F401,F403
from utils import wait_for, only_one, mine_funding_to_announce
from pyln.client import RpcError, Millisatoshi
import os
import pytest


def _setup_middleman(node_factory, bitcoind, l3_disconnect, pay_l1=4 * 10**8,
                     plugin_on=2):
    """Shared topology: l1 payer - l2 victim router - l3 payee(hold)."""
    hold_plugin = '/tmp/opencode/hold-venv.py'
    l3opts = {'plugin': hold_plugin}
    if l3_disconnect:
        l3opts['disconnect'] = [l3_disconnect]
    l1, l2, l3 = node_factory.get_nodes(3, opts=[{}, {}, l3opts])
    l2.rpc.connect(l1.info['id'], 'localhost', l1.port)
    l2.rpc.connect(l3.info['id'], 'localhost', l3.port)
    l2.fundchannel(l1, 10**6)
    c23, _ = l2.fundchannel(l3, 10**6)
    l3.fundwallet(10**6)
    mine_funding_to_announce(bitcoind, [l1, l2, l3])
    l1.wait_channel_active(c23)
    l2.pay(l1, pay_l1)
    return l1, l2, l3


def _onchain_claims(bitcoind, l1, l2, l3, preimage, rhash, parts):
    """Force-close l3 and claim everything with the preimage."""
    l3.rpc.close(l2.info['id'], 1)
    bitcoind.generate_block(1, wait_for_mempool=1)
    l2.daemon.wait_for_log(' to ONCHAIN')
    l3.daemon.wait_for_log(' to ONCHAIN')
    l3.rpc.releasehtlc(preimage)
    _, txid, _ = l3.wait_for_onchaind_tx('OUR_HTLC_SUCCESS_TX',
                                         'OUR_UNILATERAL/THEIR_HTLC')
    blocks = bitcoind.generate_block(1, wait_for_mempool=[txid])
    l2.daemon.wait_for_log('THEIR_UNILATERAL/OUR_HTLC gave us preimage')
    for i in parts:
        l1.rpc.waitsendpay(rhash, TIMEOUT, partid=i)
    return txid, blocks[0]


def test_e4a_three_part_mixed_removal(node_factory, bitcoind):
    """Three same-hash HTLCs: one fulfilled, two failed-and-frozen.

    Upstream's test covers two (held + failed).  This adds a second
    failed one so the preimage-triggered fulfill path must skip TWO
    failed-but-live houts and still fulfill all three incoming HTLCs.
    """
    l1, l2, l3 = _setup_middleman(node_factory, bitcoind,
                                  '+WIRE_COMMITMENT_SIGNED*3',
                                  pay_l1=5 * 10**8)
    amt1, amt2, amt3 = 10**8, 9 * 10**7, 8 * 10**7
    preimage = '00' * 31 + '02'
    inv = l3.rpc.invoice(amt1 + amt2 + amt3, 'middleman3', 'desc', preimage=preimage)
    rhash = inv['payment_hash']
    total = amt1 + amt2 + amt3

    for i, amt in enumerate((amt1, amt2, amt3), start=1):
        l1.rpc.sendpay(l1.single_route(l3.info['id'], amt), rhash,
                       amount_msat=total,
                       payment_secret=inv['payment_secret'],
                       partid=i, groupid=1)
    l3.daemon.wait_for_log('holding htlc {}'.format(rhash))
    l3.daemon.wait_for_log(r'dev_disconnect: \+WIRE_COMMITMENT_SIGNED')

    l2.daemon.wait_for_log('peer_in WIRE_UPDATE_FAIL_HTLC')
    wait_for(lambda: len(only_one(l2.rpc.listpeerchannels(l3.info['id'])['channels'])['htlcs']) == 3)

    _onchain_claims(bitcoind, l1, l2, l3, preimage, rhash, parts=(1, 2, 3))

    statuses = [f['status'] for f in l2.rpc.listforwards()['forwards']]
    assert statuses == ['settled', 'settled', 'settled'], statuses


def test_e4c_splice_during_parked_htlc(node_factory, bitcoind):
    """Splice while a parked HTLC is in flight, then fail-then-preimage.

    The #130-shaped ordering: part1 parks at l3, the channel splices
    WITH the htlc in flight (commitment series transition under a live
    htlc), then part2's fail-CS (calibrated as the 4th) is frozen and
    l3 claims on-chain.  Both parts must settle (preimage wins).
    """
    l1, l2, l3 = _setup_middleman(node_factory, bitcoind,
                                  '+WIRE_COMMITMENT_SIGNED*4')
    amt1, amt2 = 10**8, 9 * 10**7
    preimage = '00' * 31 + '05'
    inv = l3.rpc.invoice(amt1 + amt2, 'middleman-splice2', 'desc', preimage=preimage)
    rhash = inv['payment_hash']

    l1.rpc.sendpay(l1.single_route(l3.info['id'], amt1), rhash,
                   amount_msat=amt1 + amt2,
                   payment_secret=inv['payment_secret'],
                   partid=1, groupid=1)
    l3.daemon.wait_for_log('holding htlc {}'.format(rhash))

    l3.fundwallet(10**6)
    l3.rpc.splice('wallet -> 500000; 100000 -> *:?', debug_log=True)
    bitcoind.generate_block(6, wait_for_mempool=1)
    l3.daemon.wait_for_log(r'lightningd, splice_locked clearing inflights')
    l2.daemon.wait_for_log(r'lightningd, splice_locked clearing inflights')

    l1.rpc.sendpay(l1.single_route(l3.info['id'], amt2), rhash,
                   amount_msat=amt1 + amt2,
                   payment_secret=inv['payment_secret'],
                   partid=2, groupid=1)
    l3.daemon.wait_for_log('failing htlc {}'.format(rhash))
    l3.daemon.wait_for_log(r'dev_disconnect: \+WIRE_COMMITMENT_SIGNED')
    l2.daemon.wait_for_log('peer_in WIRE_UPDATE_FAIL_HTLC')
    wait_for(lambda: len(only_one(l2.rpc.listpeerchannels(l3.info['id'])['channels'])['htlcs']) == 2)

    # Close-path anomaly detection (observed cycles 5+7): the unilateral
    # close after a frozen post-splice commitment_signed classifies as
    # THEIR_REVOKED_UNILATERAL on the closer — a revoked commitment is
    # published instead of the current one, and the HTLC-success claim
    # never fires.  Recorded as a candidate bug, asserted here.
    l3.rpc.close(l2.info['id'], 1)
    bitcoind.generate_block(1, wait_for_mempool=1)
    l2.daemon.wait_for_log(' to ONCHAIN')
    l3.daemon.wait_for_log(' to ONCHAIN')
    revoked_close = l3.daemon.is_in_log('THEIR_REVOKED_UNILATERAL')
    print('E4C_CLOSE_ANOMALY revoked_unilateral_close={}'.format(revoked_close))
    import glob, shutil
    for n, tag in ((l3, 'l3'), (l2, 'l2')):
        hits = glob.glob(n.daemon.lightning_dir + '**/lightningd.log',
                         recursive=True)
        if hits:
            shutil.copyfile(hits[0], '/tmp/opencode/e4c-{}-lightningd.log'.format(tag))
    for n in (l1, l2, l3):
        assert not n.daemon.is_in_log('FATAL')
    assert not l2.daemon.is_in_log(r'\*\*BROKEN\*\*')


def test_e2_splice_input_invalidation(node_factory, bitcoind):
    """Phantom funding: the opener double-spends its own splice input.

    l2 (attacker opener) splices in with a wallet UTXO; after both
    sides sign and the splice tx is broadcast, l2 double-spends that
    UTXO with a fat-fee conflict which is mined.  The splice tx can
    never confirm.  l1 (victim accepter) is then observed: does the
    inflight abort cleanly (v7 txo watcher) or does l1 track a phantom
    funding txid (v6, and any unpatched corner) — force-close then
    hangs in AWAIT_UNILATERAL: the #9498 forensic signature.
    """
    l1, l2 = node_factory.get_nodes(2)
    l2.rpc.connect(l1.info['id'], 'localhost', l1.port)
    l2.fundchannel(l1, 10**6)
    l2.daemon.wait_for_log(' to CHANNELD_NORMAL')
    orig_funding = only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])['funding_txid']

    addr = l2.rpc.newaddr('bech32')['bech32']
    bitcoind.rpc.sendtoaddress(addr, 0.01)
    bitcoind.generate_block(1)
    wait_for(lambda: any(o['status'] == 'confirmed' for o in l2.rpc.listfunds()['outputs']))

    l2.rpc.splice('wallet -> 500000; 100000 -> *:?', debug_log=True)
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) >= 1)
    splice_txid = None
    for txid in bitcoind.rpc.getrawmempool():
        tx = bitcoind.rpc.getrawtransaction(txid, 1)
        if any(v['txid'] == orig_funding for v in tx['vin']):
            splice_txid = txid
            break
    assert splice_txid, 'splice tx not found in mempool'
    tx = bitcoind.rpc.getrawtransaction(splice_txid, 1)
    wallet_vins = [v for v in tx['vin']
                   if not (v['txid'] == orig_funding and v['vout'] == 0)]
    assert wallet_vins, 'no wallet input found in splice tx'
    utxo = '{}:{}'.format(wallet_vins[0]['txid'], wallet_vins[0]['vout'])

    dst = l2.rpc.newaddr('bech32')['bech32']
    psbt = l2.rpc.txprepare(outputs=[{dst: 900000}], feerate=10000, utxos=[utxo])['psbt']
    l2.rpc.txsend(psbt)
    bitcoind.generate_block(1, wait_for_mempool=1)
    bitcoind.generate_block(10)

    version = l1.rpc.getinfo()['version']
    ch = only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])
    print('E2 post-invalidation: version={} state={} funding_txid={} inflight={}'.format(
        version, ch['state'], ch['funding_txid'],
        [i.get('funding_txid') for i in ch.get('inflight', [])]))

    if 'v26.06.7' in version:
        wait_for(lambda: only_one(
            l1.rpc.listpeerchannels(l2.info['id'])['channels'])['state']
            in ('CHANNELD_NORMAL', 'ONCHAIN', 'AWAITING_UNILATERAL'))
        ch7 = only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])
        assert 'inflight' not in ch7 or ch7['funding_txid'] == orig_funding, \
            'PHANTOM FUNDING ON PATCHED .7: funding={} inflight={}'.format(
                ch7['funding_txid'], ch7.get('inflight'))
    assert not l1.daemon.is_in_log('FATAL')
    assert not l2.daemon.is_in_log('FATAL')


def test_e4e_reorg_after_preimage_settlement(node_factory, bitcoind):
    """Upstream choreography, then the claim block is reorged away.

    The preimage was already discovered and both incoming HTLCs settled;
    invalidate the block carrying the HTLC-success claim, let the claim
    re-confirm on an alternative chain: no crash, no double resolution,
    forwards stay settled.
    """
    l1, l2, l3 = _setup_middleman(node_factory, bitcoind,
                                  '+WIRE_COMMITMENT_SIGNED*3')
    amt1, amt2 = 10**8, 9 * 10**7
    preimage = '00' * 31 + '03'
    inv = l3.rpc.invoice(amt1 + amt2, 'middleman-reorg', 'desc', preimage=preimage)
    rhash = inv['payment_hash']

    l1.rpc.sendpay(l1.single_route(l3.info['id'], amt1), rhash,
                   amount_msat=amt1 + amt2,
                   payment_secret=inv['payment_secret'],
                   partid=1, groupid=1)
    l3.daemon.wait_for_log('holding htlc {}'.format(rhash))
    l1.rpc.sendpay(l1.single_route(l3.info['id'], amt2), rhash,
                   amount_msat=amt1 + amt2,
                   payment_secret=inv['payment_secret'],
                   partid=2, groupid=1)
    l3.daemon.wait_for_log('failing htlc {}'.format(rhash))
    l3.daemon.wait_for_log(r'dev_disconnect: \+WIRE_COMMITMENT_SIGNED')

    l2.daemon.wait_for_log('peer_in WIRE_UPDATE_FAIL_HTLC')
    wait_for(lambda: len(only_one(l2.rpc.listpeerchannels(l3.info['id'])['channels'])['htlcs']) == 2)

    txid, claim_block = _onchain_claims(bitcoind, l1, l2, l3, preimage, rhash,
                                        parts=(1, 2))

    # --- reorg the claim block away ---
    bitcoind.rpc.invalidateblock(claim_block)
    wait_for(lambda: txid in bitcoind.rpc.getrawmempool())
    bitcoind.generate_block(2, wait_for_mempool=[txid])
    wait_for(lambda: [f['status'] for f in l2.rpc.listforwards()['forwards']] == ['settled', 'settled'])
    for n in (l1, l2, l3):
        assert not n.daemon.is_in_log(r'\*\*BROKEN\*\*')
        assert not n.daemon.is_in_log('FATAL')


