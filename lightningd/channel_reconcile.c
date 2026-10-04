#include "config.h"
#include <bitcoin/short_channel_id.h>
#include <ccan/tal/str/str.h>
#include <lightningd/bitcoind.h>
#include <lightningd/channel.h>
#include <lightningd/channel_control.h>
#include <lightningd/channel_reconcile.h>
#include <lightningd/chaintopology.h>
#include <lightningd/connect_control.h>
#include <lightningd/io_loop_with_timers.h>
#include <lightningd/lightningd.h>
#include <lightningd/peer_control.h>
#include <wallet/wallet.h>

/* Startup reconciliation of spent-funding channels.
 *
 * A lightningd which restarts while its channel funding was already spent
 * onchain (or is being spent in the mempool) still has the channel in a
 * live DB state (CHANNELD_NORMAL etc.).  The funding spend watch only
 * concludes once the startup block rescan reaches the spending block:
 * until then channeld is free to attach and propose new commitments
 * against an already-spent funding output.  A stock HSM signs those
 * blindly; a chain-aware signer must refuse, breaking the channel.
 *
 * Before any subdaemon can attach, we ask bitcoind (one getutxout per
 * live channel) whether the funding output is still an unspent UTXO:
 *
 *  - unspent: normal startup, zero behavior change.
 *
 *  - spent, and we had previously seen the funding confirmed: the channel
 *    is marked funding_spent_unresolved, which blocks subdaemon attach
 *    (see connect_activate_subd() and handle_peer_spoke()).  The
 *    confirmed case resolves by the existing machinery: the startup
 *    rescan fires the funding spend watch, which performs
 *    FUNDING_SPEND_SEEN -> ONCHAIN (drop-to-chain) as usual, so the
 *    channel never attaches again.
 *
 *  - spent according to the chainview but not in any block (spend in
 *    mempool): RAIL DECISION - the channel takes
 *    AWAITING_UNILATERAL-style semantics: we do not attach, and no new
 *    commitment is ever requested, until either the spend confirms (the
 *    watch fires and onchaind takes over) or a later check finds the
 *    output unspent again (mempool eviction / reorg), at which point
 *    normal attachment resumes.  This is deliberately conservative in
 *    the direction of never asking the signer to commit against a
 *    funding output the chain says is gone.
 *
 * Channels whose funding we never saw confirmed (zeroconf stubs, v2
 * opening candidates) are not gated: for them a missing getutxout entry
 * means "not mined yet", not "spent", and their inflight/confirmation
 * watches already own that window.
 */

/* Context for the startup pass: we pump the io_loop until every
 * outstanding getutxout has been answered. */
struct reconcile_pending {
	struct lightningd *ld;
	size_t outstanding;
};

struct reconcile_req {
	struct reconcile_pending *pend;
	struct channel *channel;
};

static bool funding_previously_confirmed(const struct channel *channel)
{
	/* A real scid means the funding reached minimum depth. */
	if (channel->scid && !is_stub_scid(*channel->scid))
		return true;

	/* Otherwise, we may still have seen and recorded the funding
	 * transaction in a block (e.g. v1 CHANNELD_AWAITING_LOCKIN
	 * before lockin depth, or a dual funding candidate which
	 * confirmed). */
	return wallet_transaction_height(channel->peer->ld->wallet,
					 &channel->funding.txid) != 0;
}

/* The states whose subdaemons would resume a live channel: mirrors the
 * attach switch in connect_activate_subd() plus the CLOSINGD_COMPLETE
 * reestablish path in handle_peer_spoke().  DUALOPEND_OPEN_INIT /
 * _COMMIT_READY / _COMMITTED are excluded: their funding is not
 * confirmed (or not even signed), so there is nothing resolvable to
 * gate (funding_previously_confirmed() would refuse them anyway). */
static bool state_reconcile_gated(enum channel_state state)
{
	switch (state) {
	case CHANNELD_AWAITING_LOCKIN:
	case CHANNELD_NORMAL:
	case CHANNELD_AWAITING_SPLICE:
	case CHANNELD_SHUTTING_DOWN:
	case CLOSINGD_SIGEXCHANGE:
	case CLOSINGD_COMPLETE:
	case DUALOPEND_AWAITING_LOCKIN:
		return true;
	case DUALOPEND_OPEN_INIT:
	case DUALOPEND_OPEN_COMMIT_READY:
	case DUALOPEND_OPEN_COMMITTED:
	case AWAITING_UNILATERAL:
	case FUNDING_SPEND_SEEN:
	case ONCHAIN:
	case CLOSED:
		return false;
	}
	abort();
}

bool channel_funding_spend_unresolved(const struct channel *channel)
{
	return channel->funding_spent_unresolved;
}

static void mark_funding_spent(struct channel *channel)
{
	channel->funding_spent_unresolved = true;
	log_unusual(channel->log,
		    "Funding output %s is spent according to bitcoind: "
		    "not attaching subdaemons until the spend is reconciled "
		    "(onchaind takeover or chainview recovery)",
		    fmt_bitcoin_outpoint(tmpctx, &channel->funding));
	channel_set_billboard(channel, false,
			      "Funding spent onchain or in mempool: "
			      "awaiting onchain resolution before "
			      "reattaching");
}

static void startup_utxo_cb(struct bitcoind *bitcoind UNUSED,
			   const struct bitcoin_tx_output *txout,
			   struct reconcile_req *req)
{
	if (!txout && funding_previously_confirmed(req->channel))
		mark_funding_spent(req->channel);

	if (--req->pend->outstanding == 0)
		io_break(req->pend->ld);
}

/* Async recheck (main io_loop is running by then): if a deferred
 * channel's funding is an unspent UTXO again, resume normal
 * attachment. */
static void recheck_utxo_cb(struct bitcoind *bitcoind UNUSED,
			    const struct bitcoin_tx_output *txout,
			    struct channel *channel)
{
	struct lightningd *ld = channel->peer->ld;

	if (!txout || !channel->funding_spent_unresolved)
		return;

	/* Spent report overturned (mempool eviction, reorg): clear the
	 * deferral and let the normal connect machinery resume. */
	channel->funding_spent_unresolved = false;
	log_unusual(channel->log,
		    "Funding output %s is unspent again: resuming normal "
		    "channel operation",
		    fmt_bitcoin_outpoint(tmpctx, &channel->funding));

	if (ld->reconnect
	    && channel_state_wants_peercomms(channel->state)
	    && !ignore_idle_channel(ld, channel)) {
		connectd_connect_to_peer(ld, channel->peer,
					 "funding spend report overturned",
					 false);
	}
}

/* Called whenever the topology catches up with bitcoind: recheck any
 * channels still deferring on a spent report (mempool case), since a
 * confirmed spend would have fired the funding watch and moved the
 * channel onchain already. */
static void reconcile_sync_waiter(struct chain_topology *topo UNUSED,
				  struct lightningd *ld)
{
	struct peer *peer;
	struct peer_node_id_map_iter it;

	for (peer = peer_node_id_map_first(ld->peers, &it);
	     peer;
	     peer = peer_node_id_map_next(ld->peers, &it)) {
		struct channel *channel;

		list_for_each(&peer->channels, channel, list) {
			if (!channel->funding_spent_unresolved)
				continue;
			/* The watch may have already moved it onchain */
			if (!state_reconcile_gated(channel->state))
				continue;
			bitcoind_getutxout(channel, ld->topology->bitcoind,
					   &channel->funding,
					   recheck_utxo_cb, channel);
		}
	}
}

void channel_reconcile_funding(struct lightningd *ld)
{
	struct peer *peer;
	struct peer_node_id_map_iter it;
	struct reconcile_pending *pend;

	pend = tal(NULL, struct reconcile_pending);
	pend->ld = ld;
	pend->outstanding = 0;

	for (peer = peer_node_id_map_first(ld->peers, &it);
	     peer;
	     peer = peer_node_id_map_next(ld->peers, &it)) {
		struct channel *channel;

		list_for_each(&peer->channels, channel, list) {
			struct reconcile_req *req;

			if (!state_reconcile_gated(channel->state))
				continue;
			req = tal(pend, struct reconcile_req);
			req->pend = pend;
			req->channel = channel;
			pend->outstanding++;
			bitcoind_getutxout(req, ld->topology->bitcoind,
					   &channel->funding,
					   startup_utxo_cb, req);
		}
	}

	if (pend->outstanding != 0) {
		while (pend->outstanding)
			io_loop_with_timers(ld);
	}
	tal_free(pend);

	/* Recheck deferred channels whenever the topology catches up:
	 * covers both the mempool-eviction recovery and a periodic
	 * re-confirmation of the deferral.  Registered unconditionally
	 * (a no-op walk when nothing is deferred). */
	topology_add_sync_waiter(ld, ld->topology,
				 reconcile_sync_waiter, ld);
}
