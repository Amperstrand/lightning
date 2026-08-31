import time
from fixtures import *  # noqa: F401,F403
import pytest
import unittest
from pyln.testing.utils import EXPERIMENTAL_DUAL_FUND
from utils import (
    TEST_NETWORK
)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_crash_window(node_factory, bitcoind, executor):
    """Crash-window splice resume, with the convergence window able to
    converge.

    Injects the VLS crash knowledge state deterministically: l1's
    second-update commitment_signed is SIGNED by channeld but the wire
    send is ABORTED (dev_disconnect '-' = disconnect instead of sending)
    -- identical to the VLS-proxy-latency crash where the sign completed
    but the message never reached l2. l1 is then killed and restarted,
    exactly like test_commit_crash_splice.

    The R28 correction (2026-08-31): the original 60s classification
    window never mined blocks, so RESUME-COMPLETE was unreachable dead
    code and every healthy run classified as NO-CONVERGENCE — the "wedge"
    (#87, RETRACTED) was a test-design artifact. The reestablished splice
    is awaiting CONFIRMATION, not stuck: with blocks mined after the
    reestablish, the channel must reach NORMAL on both sides and carry a
    post-splice payment. Proof: wedge-retraction-proof.log (30.65s).
    """
    # Model the flow on test_splice_rbf (the verified baseline): splice out,
    # output-only psbt. l1's commitment_signed sends: [dual-fund: channel-open
    # one, then] round-1's commit#0, round-2's commit#1. We abort the LAST.
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
    print("PHASE1: update#1 accepted (no round yet)", flush=True)

    # Round(s) run during update#2's processing: l1 signs and sends
    # commit#0 then commit#1 (the psbt-grown-fee one); the latter's
    # send aborts.
    executor.submit(l1.rpc.splice_update, chan_id, result['psbt'])
    l1.daemon.wait_for_log(r'dev_disconnect: \-WIRE_COMMITMENT_SIGNED')
    print("PHASE2: round-2 commitment send ABORTED (signed, undelivered)", flush=True)
    time.sleep(0.5)

    # The crash: kill l1 in the signed-but-undelivered state.
    l1.daemon.kill()
    print("PHASE3: l1 killed", flush=True)

    # Restart clean, reconnect.
    del l1.daemon.opts['dev-no-reconnect']
    del l1.daemon.opts['dev-disconnect']
    l1.start()

    l1.daemon.wait_for_log(r'peer_in WIRE_CHANNEL_REESTABLISH')
    l2.daemon.wait_for_log(r'peer_in WIRE_CHANNEL_REESTABLISH')
    print("PHASE4: reestablished", flush=True)

    # THE CORRECTION STEP (R28): the resumed splice is a real inflight
    # awaiting confirmation — the exchange completes at reestablish and
    # the splice tx lands in the mempool. A window that never mines
    # classifies healthy runs as wedged (dead-code criteria).
    wait_for(lambda: len(list(bitcoind.rpc.getrawmempool(True).keys())) >= 1)
    print("PHASE5: splice tx in mempool, mining 6 blocks", flush=True)
    bitcoind.generate_block(6, wait_for_mempool=1)

    # Convergence is now REACHABLE and REQUIRED: both sides NORMAL.
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL', 30)
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL', 30)
    print("OUTCOME: RESUME-COMPLETE with mining — R28 correction verified", flush=True)

    # Full end-to-end proof: post-splice payment, no unilateral close.
    inv = l2.rpc.invoice(10**2, 'c1', 'crash_window')
    l1.rpc.xpay(inv['bolt11'])
    time.sleep(5)
    assert l1.db_query("SELECT count(*) as c FROM channeltxs;")[0]['c'] == 0
    print("OUTCOME: post-splice payment OK, no unilateral close", flush=True)

    # A genuine rejection loop ('Bad commit_sig' — the VLS crash-resume
    # class this test was born to catch) fails the convergence assert
    # above by timeout; surface it in the log for triage.
    for name, node in (('l1', l1), ('l2', l2)):
        if node.daemon.is_in_log(r'Bad commit_sig'):
            print(f"TRIAGE: {name} hit 'Bad commit_sig' — the rejection-loop class", flush=True)

    # Durable evidence: full daemon logs survive teardown (pytest deletes
    # passing-run test dirs). UNIQUE per run: the fixed shared path was a
    # cross-session contamination vector (a parallel invocation overwrote
    # run 4's dumps mid-analysis — STATE.md "CROSS-RUN CONTAMINATION").
    import os
    dumpdir = f"/tmp/crash-window-logs-{int(time.time())}"
    os.makedirs(dumpdir, exist_ok=True)
    with open(dumpdir + '/l1.log', 'w') as f:
        f.write('\n'.join(l1.daemon.logs))
    with open(dumpdir + '/l2.log', 'w') as f:
        f.write('\n'.join(l2.daemon.logs))
    print(f"LOGS: {dumpdir}/l1.log,l2.log", flush=True)
