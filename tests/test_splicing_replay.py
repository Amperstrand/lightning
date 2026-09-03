"""Fork-only tests (inr2-splice-harness #125): the splice crash-resume
must REPLAY the durably stored commitment_signed bytes, never re-sign.

1. test_replay_resume_replays_not_resigns: the crash-window shape with an
   explicit replay assert — the resumed node must log the replay and still
   converge (under a validating signer, a re-sign would be refused and the
   convergence assert would time out: that is exactly the pre-fix failure).
2. test_replay_peer_already_received: double-delivery — l1 sends its
   commitment_signed batch ('+' closes after send) while l2 dies on
   receiving it ('<' closes after recv, before durable processing). On
   mutual restart l2's reestablish flags request retransmission and l1
   replays bytes l2 had transiently seen: a retransmitted
   commitment_signed the peer already observed must be tolerated (the
   store-then-send design makes this window real).
3. test_replay_multiround_crash: rapid sequential splice rounds (the
   concurrent-rounds flow) with the SECOND round's commitment send
   aborted: the resume replays at an ADVANCED commitment number, with
   confirmed-round history behind it — exercising the store key-space
   across numbers.
"""
import time
from fixtures import *  # noqa: F401,F403
import pytest
import unittest
from pyln.testing.utils import EXPERIMENTAL_DUAL_FUND, wait_for
from utils import (
    TEST_NETWORK
)


def _mine_and_converge(bitcoind, l1, l2, timeout=60):
    """The R28 correction: the resumed splice is a real inflight awaiting
    confirmation — mine, then BOTH sides must reach NORMAL."""
    wait_for(lambda: len(list(bitcoind.rpc.getrawmempool(True).keys())) >= 1)
    bitcoind.generate_block(6, wait_for_mempool=1)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL', timeout)
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL', timeout)


def _assert_no_rejection_loop(l1, l2):
    """The pre-fix signature: a re-sign during resume makes the peer (or a
    validating signer) reject in a loop."""
    for name, node in (('l1', l1), ('l2', l2)):
        assert not node.daemon.is_in_log(r'Bad commit_sig'), \
            f"{name} hit the rejection-loop class"


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'regtest-only')
def test_replay_resume_replays_not_resigns(node_factory, bitcoind, executor):
    """The #125 headline: on crash-resume the stored bytes are REPLAYED
    (fork log line) and the channel converges with a post-splice payment."""
    if EXPERIMENTAL_DUAL_FUND:
        disconnects = ['-WIRE_COMMITMENT_SIGNED*3']
    else:
        disconnects = ['-WIRE_COMMITMENT_SIGNED']

    l1 = node_factory.get_node(disconnect=disconnects,
                               options={'dev-no-reconnect': None},
                               may_reconnect=True)
    l2 = node_factory.get_node(may_reconnect=True)
    l1.openchannel(l2, 1000000)
    chan_id = l1.get_channel_id(l2)

    funds_result = l1.rpc.addpsbtoutput(100000)
    result = l1.rpc.splice_init(chan_id, -105801, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False

    # Round(s) run during update#2: the commitment batch is signed and
    # stored; the wire send aborts (dev_disconnect '-').
    executor.submit(l1.rpc.splice_update, chan_id, result['psbt'])
    l1.daemon.wait_for_log(r'dev_disconnect: \-WIRE_COMMITMENT_SIGNED')
    time.sleep(0.5)
    l1.daemon.kill()

    del l1.daemon.opts['dev-no-reconnect']
    del l1.daemon.opts['dev-disconnect']
    l1.start()

    l1.daemon.wait_for_log(r'peer_in WIRE_CHANNEL_REESTABLISH')
    l2.daemon.wait_for_log(r'peer_in WIRE_CHANNEL_REESTABLISH')

    # THE REPLAY ASSERT: the resumed node must replay the stored bytes.
    l1.daemon.wait_for_log(r'Splice resume: replaying [0-9]+ stored '
                           r'commitment_signed msgs', 30)

    _mine_and_converge(bitcoind, l1, l2)

    inv = l2.rpc.invoice(10**2, 'rp1', 'replay1')
    l1.rpc.xpay(inv['bolt11'])
    time.sleep(5)
    assert l1.db_query("SELECT count(*) as c FROM channeltxs;")[0]['c'] == 0
    _assert_no_rejection_loop(l1, l2)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'regtest-only')
def test_replay_peer_already_received(node_factory, bitcoind, executor):
    """Double-delivery: l1 sends the batch then dies ('+'), l2 dies on
    receiving it ('<'). After mutual restart, retransmission must be
    tolerated even though l2 had transiently seen the message."""
    if EXPERIMENTAL_DUAL_FUND:
        n = 3
    else:
        n = 1

    l1 = node_factory.get_node(disconnect=[f'+WIRE_COMMITMENT_SIGNED*{n}'],
                               options={'dev-no-reconnect': None},
                               may_reconnect=True)
    l2 = node_factory.get_node(disconnect=[f'<WIRE_COMMITMENT_SIGNED*{n}'],
                               options={'dev-no-reconnect': None},
                               may_reconnect=True)
    l1.openchannel(l2, 1000000)
    chan_id = l1.get_channel_id(l2)

    funds_result = l1.rpc.addpsbtoutput(100000)
    result = l1.rpc.splice_init(chan_id, -105801, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])

    executor.submit(l1.rpc.splice_update, chan_id, result['psbt'])
    # Both disconnects fire on the same exchanged message; wait for ours.
    l1.daemon.wait_for_log(r'dev_disconnect: \+WIRE_COMMITMENT_SIGNED')
    l2.daemon.wait_for_log(r'dev_disconnect: \<WIRE_COMMITMENT_SIGNED')
    time.sleep(0.5)
    l1.daemon.kill()
    l2.daemon.kill()

    del l1.daemon.opts['dev-no-reconnect']
    del l1.daemon.opts['dev-disconnect']
    del l2.daemon.opts['dev-no-reconnect']
    del l2.daemon.opts['dev-disconnect']
    l1.start()
    l2.start()

    l1.daemon.wait_for_log(r'peer_in WIRE_CHANNEL_REESTABLISH')
    l2.daemon.wait_for_log(r'peer_in WIRE_CHANNEL_REESTABLISH')

    _mine_and_converge(bitcoind, l1, l2)

    inv = l2.rpc.invoice(10**2, 'rp2', 'replay2')
    l1.rpc.xpay(inv['bolt11'])
    time.sleep(5)
    assert l1.db_query("SELECT count(*) as c FROM channeltxs;")[0]['c'] == 0
    _assert_no_rejection_loop(l1, l2)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'regtest-only')
def test_replay_multiround_crash(node_factory, bitcoind, executor):
    """Rapid sequential rounds; round 2's commitment send aborts. The
    resume replays at an ADVANCED number with confirmed-round history."""
    # NOTE: replayed writes also pass through the dev_disconnect hook and
    # consume list entries (earned in this test's first version: a reestablish
    # replay ate an entry and shifted the abort point). So: a run of passes
    # then the abort — whatever the pre-target write count (round-0 batches,
    # fee-update commits, replays), the abort lands mid-round-1.
    disconnects = ['=WIRE_COMMITMENT_SIGNED', '-WIRE_COMMITMENT_SIGNED']

    # No wait_for_announce: multi-round + announcements reproduces the #124
    # announce-vs-negotiation race (observed live in this test's 2nd version).
    l1 = node_factory.get_node(disconnect=disconnects,
                               options={'dev-no-reconnect': None},
                               may_reconnect=True)
    l2 = node_factory.get_node(may_reconnect=True)
    l1.openchannel(l2, 1000000)
    chan_id = l1.get_channel_id(l2)

    # Round 0 (splice OUT, the concurrent-rounds shape): completes fully
    # (its commitment_signed consumes the '=') and confirms.
    funds_result = l1.rpc.addpsbtoutput(50000)
    result = l1.rpc.splice_init(chan_id, -55801, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    try:
        signed = l1.rpc.signpsbt(result['psbt'])['signed_psbt']
    except Exception:
        signed = result['psbt']
    result = l1.rpc.splice_signed(chan_id, signed)
    bitcoind.generate_block(6, wait_for_mempool=1)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL',
                           timeout=60)

    # Let round-0's announcement exchange settle BEFORE round 1's STFU:
    # a mid-negotiation WIRE_ANNOUNCEMENT_SIGNATURES is the #124 race
    # (load-biased, both arms). This test is the #130 repro, not the
    # #124 vehicle — decouple them.
    l1.daemon.wait_for_log(r'peer_in WIRE_ANNOUNCEMENT_SIGNATURES', 60)
    l2.daemon.wait_for_log(r'peer_in WIRE_ANNOUNCEMENT_SIGNATURES', 60)

    # Round 1 (splice IN — the proven alternating shape; a second OUT
    # round trips CLN's funding accounting): the abort fires mid-round.
    funds_result = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 50000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    executor.submit(l1.rpc.splice_update, chan_id, result['psbt'])
    l1.daemon.wait_for_log(r'dev_disconnect: \-WIRE_COMMITMENT_SIGNED')
    time.sleep(0.5)
    l1.daemon.kill()

    del l1.daemon.opts['dev-no-reconnect']
    del l1.daemon.opts['dev-disconnect']
    l1.start()

    # fork #130 contract: with user sigs still pending (the crash ate the
    # signpsbt step), the resume PARKS at the signature phase — but the
    # commitment exchange completes, l1 REPLAYING its stored round-1
    # batch (never re-signing; the pre-fix code dropped the next_funding
    # TLV and mis-aborted the peer's valid resume as "not recognized").
    # The order is chronological: pyln wait_for_log only matches forward
    # from a cursor, and on l1 the park line (reestablish prep) precedes
    # the reestablish exchange, which precedes the replay.
    l1.daemon.wait_for_log(r'park at the signature phase', 30)
    l1.daemon.wait_for_log(r'peer_in WIRE_CHANNEL_REESTABLISH')
    l2.daemon.wait_for_log(r'peer_in WIRE_CHANNEL_REESTABLISH')
    l1.daemon.wait_for_log(r'Splice resume: replaying [0-9]+ stored '
                           r'commitment_signed msgs', 30)

    # The interrupted user step, completed post-restart: sign our wallet
    # input and submit — this un-parks the negotiation and the splice
    # completes (the real-world journey for a crash mid-signing).
    signed = l1.rpc.signpsbt(result['psbt'])['signed_psbt']
    l1.rpc.splice_signed(chan_id, signed)

    _mine_and_converge(bitcoind, l1, l2)

    inv = l2.rpc.invoice(10**2, 'rp3', 'replay3')
    l1.rpc.xpay(inv['bolt11'])
    time.sleep(5)
    assert l1.db_query("SELECT count(*) as c FROM channeltxs;")[0]['c'] == 0
    _assert_no_rejection_loop(l1, l2)
