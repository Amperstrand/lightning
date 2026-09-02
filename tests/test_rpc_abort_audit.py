"""Discriminator probes for the 2026-09-02 RPC-reachable assert/abort
audit (docs/cln-assert-audit-2026-09-02.md in lightning-playground).

Same discipline as the #9455 campaign: exact-value boundary arithmetic
hand-derived BEFORE the run, every path asserts daemon liveness
(`getinfo()['id']`), and on the fix branch every refusal is a TYPED
RpcError — the cross-implementation doctrine (lnd, eclair and electrum
never abort on caller-induced wallet/param corners).

RED (vanilla master c1551c557): each probe kills lightningd with
SIGABRT (assert/abort in the encode/report path) — that outcome is a
FINDING.  GREEN (fix branch `audit/rpc-abort-probes`): typed refusal,
daemon alive.
"""

from fixtures import *  # noqa: F401,F403
from pyln.client import RpcError

import pytest


def test_invoice_expiry_over_2p60_typed_refusal(node_factory):
    """Audit finding A: the `invoice` RPC takes `expiry` as an
    unclamped param_u64 (lightningd/invoice.c) and stores it into
    b11->expiry, which is encoded by push_varlen_field() — an abort()
    for any value that needs more than 60 bits (common/bolt11.c:1114).

    Boundary arithmetic, exactly: the encoder loop tries nbits = 5,
    10, ..., 60 and aborts when none holds.  expiry = 2**60 - 1 needs
    exactly 60 bits -> encodes fine.  expiry = 2**60 needs 61 bits ->
    vanilla aborts the daemon; fixed build must refuse with the typed
    JSONRPC2_INVALID_PARAMS.
    """
    l1 = node_factory.get_node()

    # CONTROL ARM: normal expiry — proves the invoice path itself works
    ctrl = l1.rpc.call('invoice', {
        'amount_msat': '1000msat',
        'label': 'audit-expiry-control',
        'description': 'audit control',
        'expiry': 3600,
    })
    assert ctrl['bolt11']
    print("CONTROL-ARM-OK")
    assert l1.rpc.getinfo()['id']

    # exact lower boundary: 2**60 - 1 fits in 60 bits and must work
    ok = l1.rpc.call('invoice', {
        'amount_msat': '1000msat',
        'label': 'audit-expiry-boundary-ok',
        'description': 'audit boundary',
        'expiry': 2**60 - 1,
    })
    assert ok['bolt11']
    assert l1.rpc.getinfo()['id']

    # one exponent up the encoder cannot fit the value: typed refusal,
    # never an abort
    with pytest.raises(RpcError, match=r'expiry') as err:
        l1.rpc.call('invoice', {
            'amount_msat': '1000msat',
            'label': 'audit-expiry-boundary-kill',
            'description': 'audit boundary kill',
            'expiry': 2**60,
        })
    assert err.value.error['code'] == -32602

    # the daemon survived the call — the #9452-class discriminator
    assert l1.rpc.getinfo()['id']


def test_openchannel_bump_non_dualopen_typed_refusal(node_factory):
    """Audit finding B: json_openchannel_bump() computes the BOLT-2
    25/24 feerate ramp BEFORE the state gates:

        last_feerate_perkw = channel_last_funding_feerate(channel);
        next_feerate_min   = last_feerate_perkw * 25 / 24;
        assert(next_feerate_min > last_feerate_perkw);

    channel_last_funding_feerate() returns 0 for any channel without an
    inflight — every V1 fundchannel channel — so the assert evaluates
    `0 > 0` and SIGABRTs the daemon, one screen above the honest typed
    errors ("Channel not eligible to init RBF", "No inflight for this
    channel exists") that were written for exactly this situation.

    Fixture: a plain V1 channel (no inflights), a well-formed request
    (PSBT inputs cover `amount`, so the earlier value checks pass and
    the crash is reached by control flow, not by malformed input).
    Fixed build must return FUNDING_STATE_INVALID (312) and stay alive.
    """
    l1, l2 = node_factory.get_nodes(2)
    l1.fundchannel(l2, 1_000_000, wait_for_active=True)
    cid = l1.rpc.listpeerchannels()['channels'][0]['channel_id']

    # well-formed PSBT with input value > amount: passes the request
    # sanity gates that run before the channel-state logic
    f = l1.rpc.fundpsbt(satoshi='500000sat', feerate='253perkw',
                        startweight=100)
    assert f['psbt']

    with pytest.raises(RpcError,
                       match=r'not eligible|No inflight') as err:
        l1.rpc.call('openchannel_bump', {
            'channel_id': cid,
            'amount': '400000sat',
            'initialpsbt': f['psbt'],
        })
    assert err.value.error['code'] == 312  # FUNDING_STATE_INVALID

    # the daemon survived the call — the discriminator
    assert l1.rpc.getinfo()['id']
