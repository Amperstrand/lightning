"""HYP-2 live adjudication (#253, roadmap Phase 1): a LEASED channel that
splices has its tracked blockheight state reset to 0 at lock, and the
reset is TRANSIENT — the opener's next update_blockheight re-syncs it.

Run-1 evidence sharpened the frozen predictions (recorded, not ignored):
the post-lock value observed AFTER a round is the re-synced current
height (129), not 0. The vulnerable window is splice-lock -> first
post-lock commitment round. Behavioral three-arm differential:

- arm A (l1/l2, spliced, opener stopped IMMEDIATELY after lock):
  lessor's tracked opener-height = 0 -> falls-behind check fires once
  tip > 1008 (channel_control.c:160-180) -> permanent failure.
- arm B (c1/c2, no splice, opener stopped at the same chain point):
  tracked = open height H0 -> H0 + 1008 > 1020 -> survives.
- arm D (d1/d2, spliced, ONE post-lock round before stopping):
  tracked re-synced -> survives (proves the window is the reset, not
  the splice per se).
"""
from fixtures import *  # noqa: F401,F403
from utils import *  # noqa: F401,F403
from utils import wait_for, only_one, sync_blockheight
import re


LEASE_OPTS = {'funder-policy': 'match', 'funder-policy-mod': 100,
              'lease-fee-base-sat': '100sat', 'lease-fee-basis': 100}


def _open_leased(bitcoind, opener, lessor, amount=500000):
    opener.rpc.connect(lessor.info['id'], 'localhost', lessor.port)
    rates = opener.rpc.dev_queryrates(lessor.info['id'], amount, amount)
    opener.rpc.fundchannel(lessor.info['id'], amount, request_amt=amount,
                           feerate='2000perkw',
                           compact_lease=rates['compact_lease'])
    bitcoind.generate_block(6, wait_for_mempool=1)
    opener.daemon.wait_for_log(' to CHANNELD_NORMAL')
    lessor.daemon.wait_for_log(' to CHANNELD_NORMAL')


def _round(payer, payee, n):
    inv = payee.rpc.invoice(10**4, 'hyp2-{}'.format(n), 'h')
    payer.rpc.pay(inv['bolt11'])


@pytest.mark.openchannel('v2')
def test_hyp2_lease_splice_height_reset(node_factory, bitcoind):
    # arm A's opener cuts the link the instant it sends splice_locked:
    # the post-lock re-sync commitment_signed (which carries its
    # update_blockheight) never flows — the attacker-controlled window
    # (run5 proved the benign flow re-syncs within the lock exchange).
    l1, l2, c1, c2, d1, d2 = node_factory.get_nodes(
        6, opts=[dict(LEASE_OPTS, developer=None,
                      disconnect=['+WIRE_SPLICE_LOCKED'],
                      **{'dev-no-reconnect': None}),
                 dict(LEASE_OPTS, **{'dev-no-reconnect': None}),
                 dict(LEASE_OPTS, may_reconnect=True),
                 dict(LEASE_OPTS, may_reconnect=True),
                 dict(LEASE_OPTS, may_reconnect=True),
                 dict(LEASE_OPTS, may_reconnect=True)])
    for opener, lessor in ((l1, l2), (c1, c2), (d1, d2)):
        opener.fundwallet(20 * 10**6)
        lessor.fundwallet(20 * 10**6)
        _open_leased(bitcoind, opener, lessor)
    h0 = bitcoind.rpc.getblockchaininfo()['blocks']
    print('HYP2 open height H0={}'.format(h0))
    assert h0 + 1008 > 1020 + 10, 'control tolerance margin'

    # P1 pre-state on all three: tracked opener height > 0
    bitcoind.generate_block(3)
    sync_blockheight(bitcoind, [l1, l2, c1, c2, d1, d2])
    for n, (opener, lessor) in enumerate(((l1, l2), (c1, c2), (d1, d2))):
        _round(opener, lessor, 'pre{}'.format(n))
        line = opener.daemon.is_in_log(
            r'got commitsig \d+:.*blockheight: [1-9]')
        print('HYP2-P1 pair{} pre-splice nonzero-height commitsig: {}'.format(
            n, bool(line)))
        assert line, 'P1 failed for pair {}'.format(n)

    # splices on arms A and D; B stays untouched
    for lessor in (l2, d2):
        lessor.fundwallet(10**6)
        lessor.rpc.splice('wallet -> 500000; 100000 -> *:?', debug_log=True)
    # mine-until-locked (run8 lesson: fundwallet blocks may already have
    # confirmed a splice, so a mempool-count wait starves; count blocks,
    # never mempool, and cap the loop)
    for _ in range(12):
        locked = [n.daemon.is_in_log(r'splice_locked clearing inflights')
                  for n in (l1, l2, d1, d2)]
        if all(locked):
            break
        bitcoind.generate_block(1)
    for n in (l1, l2, d1, d2):
        assert n.daemon.is_in_log(r'splice_locked clearing inflights'), n.info['id']

    # arm D: ONE post-lock round re-syncs the tracked height
    _round(d1, d2, 'resync')
    assert d1.daemon.is_in_log(r'got commitsig \d+:.*blockheight: 1\d\d')

    # arms: stop the openers NOW (A: in the reset window; B: control;
    # D: re-synced). 20 blocks so all lessors see the disconnect.
    bitcoind.generate_block(20)
    sync_blockheight(bitcoind, [l2, c2, d2])
    for opener in (l1, c1, d1):
        opener.stop()

    # tip -> 1020: A's tolerance collapsed to 0+1008; B/D keep H0+1008
    now = bitcoind.rpc.getblockchaininfo()['blocks']
    bitcoind.generate_block(1020 - now)
    sync_blockheight(bitcoind, [l2, c2, d2])

    l2.daemon.wait_for_log('Offline peer is too far behind, terminating')
    print('HYP2-P3 arm A (spliced, reset window): LESSOR FAILED THE CHANNEL')
    time.sleep(10)
    assert not c2.daemon.is_in_log('Offline peer is too far behind'), 'B failed?!'
    print('HYP2-P3 arm B (no splice): survives at same tip')
    assert not d2.daemon.is_in_log('Offline peer is too far behind'), 'D failed?!'
    print('HYP2-P3 arm D (spliced + one round): survives — window is the reset')
