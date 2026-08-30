from fixtures import *  # noqa: F401,F403
import pytest
import unittest
import time
from pyln.testing.utils import EXPERIMENTAL_DUAL_FUND
from utils import (
    TEST_NETWORK
)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_crash_window(node_factory, bitcoind, executor):
    """Stock repro probe for the VLS splice crash-resume rejection loop.

    Injects the VLS crash knowledge state deterministically: l1's
    second-update commitment_signed is SIGNED by channeld but the wire
    send is ABORTED (dev_disconnect '-' = disconnect instead of sending)
    -- identical to the VLS-proxy-latency crash where the sign completed
    but the message never reached l2. l1 is then killed and restarted,
    exactly like test_commit_crash_splice.

    Outcome classification (all three are legitimate results):
      RESUME-COMPLETE  -- stock retransmission realigns, splice lives
      TX_ABORT-RESCUE  -- stock abandons the splice (disconnect_commit's
                          designed rescue)
      LOOP             -- repeated 'Bad commit_sig' rejections: the VLS
                          behavior reproduced on STOCK (the filing repro)
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
    print("PHASE4: reestablished", flush=True)

    # Classification window.
    deadline = time.time() + 60
    outcome = 'NO-CONVERGENCE-60s (the quiet stall)'
    while time.time() < deadline:
        if l2.daemon.is_in_log(r'Bad commit_sig') or l1.daemon.is_in_log(r'Bad commit_sig'):
            outcome = 'LOOP (Bad commit_sig on stock -- THE REPRO)'
            break
        if (l1.daemon.is_in_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
                and l2.daemon.is_in_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')):
            outcome = 'RESUME-COMPLETE'
            break
        if (l1.daemon.is_in_log(r'TX_ABORT') or l2.daemon.is_in_log(r'TX_ABORT')):
            outcome = 'TX_ABORT-RESCUE'
            break
        time.sleep(1)

    print(f"OUTCOME: {outcome}", flush=True)

    # Durable evidence: full daemon logs survive teardown (pytest deletes
    # passing-run test dirs).
    import os
    os.makedirs('/tmp/crash-window-logs', exist_ok=True)
    with open('/tmp/crash-window-logs/l1.log', 'w') as f:
        f.write('\n'.join(l1.daemon.logs))
    with open('/tmp/crash-window-logs/l2.log', 'w') as f:
        f.write('\n'.join(l2.daemon.logs))
    print("LOGS: /tmp/crash-window-logs/{l1,l2}.log", flush=True)

    # Evidence dump: the exchange-state markers from both sides.
    for name, node in (('l1', l1), ('l2', l2)):
        logs = node.daemon.logs
        markers = [l for l in logs if 'handle_peer_commit_sig(' in l
                   or 'Splice initiator' in l
                   or 'Bad commit_sig' in l
                   or 'TX_ABORT' in l
                   or 'dev_disconnect' in l
                   or 'reestablish' in l.lower()]
        print(f"=== {name} markers ({len(markers)}):", flush=True)
        for m in markers[-25:]:
            print(f"  {m}", flush=True)

    # Final channel state for the record.
    print(f"PEERSTATE l1: {l1.rpc.listpeerchannels()['channels'][0]['state'] if l1.rpc.listpeerchannels()['channels'] else 'none'}", flush=True)
    print(f"PEERSTATE l2: {l2.rpc.listpeerchannels()['channels'][0]['state'] if l2.rpc.listpeerchannels()['channels'] else 'none'}", flush=True)
