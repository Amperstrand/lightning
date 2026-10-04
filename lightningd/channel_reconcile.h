#ifndef LIGHTNING_LIGHTNINGD_CHANNEL_RECONCILE_H
#define LIGHTNING_LIGHTNINGD_CHANNEL_RECONCILE_H
#include "config.h"
#include <lightningd/lightningd.h>

/* Startup reconciliation of spent-funding channels: call once after
 * channels are loaded from the database and BEFORE connectd_activate(),
 * so no peer connection can attach a subdaemon to a channel whose
 * funding output bitcoind already reports as spent.  See
 * lightningd/channel_reconcile.c for the semantics and the mempool
 * rail decision. */
void channel_reconcile_funding(struct lightningd *ld);

/* True while a channel's funding is reported spent by bitcoind but the
 * onchain takeover has not (yet) happened: subdaemon attach must be
 * blocked (the funding spend watch will move the channel onchain once
 * the spend confirms; a chainview recovery clears this). */
bool channel_funding_spend_unresolved(const struct channel *channel);

#endif /* LIGHTNING_LIGHTNINGD_CHANNEL_RECONCILE_H */
