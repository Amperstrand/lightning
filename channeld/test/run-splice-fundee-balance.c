/* Unit rails for the splice fundee-balance contract (3e03d2aac +
 * EC-2 adoption + D1, lightning-playground #268): the hsmd
 * setup_channel push_value must carry the FUNDEE'S TOTAL POST-SPLICE
 * BALANCE (the D1 absolute-balance convention, owner decision
 * 2026-09-30) - the fundee's pre-splice owed[] balance, plus HTLCs
 * pending-at-setup attributable to the fundee (the check_balances
 * pending_htlcs[] shape), plus their sat-denominated signed relative,
 * selected by CHANNEL opener role (the fundee can be the splice
 * initiator). Under-reporting (raw amount_msat() wrap = 1000x,
 * contribution-only = missing carried balance, missing pending term)
 * makes a validating signer refuse the honest first new-era
 * commitment and wedge the splice; over-drawing (settled + pending +
 * relative below zero) is a peer failure.
 *
 * Rails (SPLICING-AUDIT POLICY, #125 run-sent_commitsigs.c pattern):
 *   MUST-ACCEPT (role mapping): both splice-initiator shapes read the
 *     FENDEE's contribution field (channel-role selection, not splice
 *     role).
 *   MUST-ACCEPT (conversion): 100000-sat contribution on a zero fundee
 *     balance reaches the hsmd wire as push_value = 100_000_000 msat -
 *     asserted byte-exact against a towire rebuild (real wiregen).
 *   MUST-ACCEPT (arithmetic): owed + signed contribution lands exactly;
 *     a withdrawal beyond the balance peer-fails "out of range".
 *   MUST-ACCEPT (D1 pending term): owed + fundee-owned pending HTLCs +
 *     signed contribution lands exactly; HTLCs owned by the OTHER side
 *     never count toward the fundee total.
 *   MUST-REFUSE (fingerprint): for nonzero contribution X sat on zero
 *     owed, the reported field is X*1000 msat, never X (the 1000x wrap).
 *   MUST-REFUSE (overflow): a contribution that overflows the msat add
 *     peer-fails "out of range for fundee balance".
 *   MUST-REFUSE (true negative total): settled + pending + relative
 *     below zero is a genuine over-draw - refused even when pending
 *     HTLCs cushion part of the withdrawal.
 */
#include "config.h"
#include <fcntl.h>
#include <inttypes.h>
#include <setjmp.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

/* channeld.c owns the subdaemon main(); rename so this file owns main. */
#define main channeld_main
#include "../channeld.c"
#undef main

#include <ccan/array_size/array_size.h>
#include <ccan/take/take.h>
#include <common/channel_type.h>
#include <common/peer_failed.h>
#include <common/setup.h>
#include <common/utils.h>
#include <wire/wire_sync.h>

/* Boundary overrides: the hsmd wire (fd == HSM_FD) is captured for the
 * must-accept contract assert; every other fd the real peer_failed chain
 * writes to (peer warning pipe, status fd) is written through or
 * black-holed so the chain runs unmodified to its exit(0x80|reason). */
#define CAPTURE_MAX 4096
static u8 captured_hsmd_msg[CAPTURE_MAX];
static size_t captured_hsmd_len;

bool wire_sync_write(int fd, const void *msg TAKES)
{
	const u8 *raw = msg;
	size_t len = tal_count(raw);

	if (fd == HSM_FD) {
		assert(len <= CAPTURE_MAX);
		memcpy(captured_hsmd_msg, raw, len);
		captured_hsmd_len = len;
		if (taken(msg))
			tal_free((void *)msg);
		return true;
	}
	/* Best-effort raw write: the refusal rails substring-search the
	 * warning payload on the peer pipe. */
	(void)!write(fd, raw, len);
	if (taken(msg))
		tal_free((void *)msg);
	return true;
}

u8 *wire_sync_read(const tal_t *ctx, int fd)
{
	assert(fd == HSM_FD);
	return towire_hsmd_setup_channel_reply(ctx);
}

static struct peer *make_splice_peer(const s64 accepter_relative,
				     const s64 opener_relative,
				     const enum side channel_opener,
				     const u64 fundee_owed_msat)
{
	struct peer *peer = talz(tmpctx, struct peer);

	peer->splicing = talz(peer, struct splicing);
	peer->splicing->accepter_relative = accepter_relative;
	peer->splicing->opener_relative = opener_relative;

	peer->channel = talz(peer, struct channel);
	peer->channel->opener = channel_opener;
	peer->channel->type = channel_type_none_obsolete(peer->channel);
	/* The real splice path always has a live htlc map; the D1 total
	 * iterates it (check_balances' pending_htlcs[] shape). */
	peer->channel->htlcs = new_htable(peer->channel, htlc_map);
	/* towire_pubkey validates, so the remote keys must be real. */
	for (size_t i = 0; i < 5; i++) {
		struct secret sk;
		struct pubkey pk;

		memset(&sk, i + 1, sizeof(sk));
		assert(pubkey_from_secret(&sk, &pk));
		switch (i) {
		case 0:
			peer->channel->basepoints[REMOTE].revocation = pk;
			break;
		case 1:
			peer->channel->basepoints[REMOTE].payment = pk;
			break;
		case 2:
			peer->channel->basepoints[REMOTE].htlc = pk;
			break;
		case 3:
			peer->channel->basepoints[REMOTE].delayed_payment = pk;
			break;
		case 4:
			peer->channel->funding_pubkey[REMOTE] = pk;
			break;
		}
	}
	/* The fundee is the side that did not open the channel. */
	peer->channel->view[LOCAL].owed[channel_opener == LOCAL
				       ? REMOTE : LOCAL]
		= amount_msat(fundee_owed_msat);
	return peer;
}

/* Install a pending HTLC owned by `owner` — the check_balances
 * pending_htlcs[] shape: bucketed purely by htlc_owner() side, amount
 * in msat. Fully-acked states (SENT_ADD_ACK_REVOCATION -> LOCAL,
 * RCVD_ADD_ACK_REVOCATION -> REMOTE) satisfy htlc_state_owner()'s
 * flag assert; note the enum's SENT/RCVD prefixes alone do NOT pick
 * the owner. */
static void add_pending_htlc(struct peer *peer, u64 id,
			     struct amount_msat amount, enum side owner)
{
	struct htlc *htlc = talz(peer->channel, struct htlc);

	htlc->id = id;
	htlc->amount = amount;
	htlc->state = owner == LOCAL ? SENT_ADD_ACK_REVOCATION
				     : RCVD_ADD_ACK_REVOCATION;
	htlc_map_add(peer->channel->htlcs, htlc);
}

/* MUST-ACCEPT (role mapping + conversion): all four opener x initiator
 * shapes read the FUNDEE's contribution and convert sat -> msat exactly. */
static void test_must_accept_conversion(void)
{
	struct amount_msat res;

	/* Channel opener REMOTE: fundee LOCAL, and the fundee initiates
	 * (we are TX_INITIATOR) => fundee IS the splice initiator =>
	 * opener_relative. */
	res = relative_splice_balance_fundee(
		make_splice_peer(0, 100000, REMOTE, 0),
		TX_INITIATOR, NULL, 0, 0);
	assert(res.millisatoshis == 100000 * 1000);

	/* Channel opener LOCAL: fundee REMOTE, remote initiates (we are
	 * TX_ACCEPTER) => fundee IS the splice initiator =>
	 * opener_relative. */
	res = relative_splice_balance_fundee(
		make_splice_peer(0, 100000, LOCAL, 0),
		TX_ACCEPTER, NULL, 0, 0);
	assert(res.millisatoshis == 100000 * 1000);

	/* Channel opener REMOTE, remote opener initiates (we, fundee
	 * LOCAL, are TX_ACCEPTER) => fundee is NOT the initiator =>
	 * accepter_relative. */
	res = relative_splice_balance_fundee(
		make_splice_peer(100000, 0, REMOTE, 0),
		TX_ACCEPTER, NULL, 0, 0);
	assert(res.millisatoshis == 100000 * 1000);

	/* Channel opener LOCAL (we initiate, fundee REMOTE is not the
	 * initiator) => accepter_relative. */
	res = relative_splice_balance_fundee(
		make_splice_peer(100000, 0, LOCAL, 0),
		TX_INITIATOR, NULL, 0, 0);
	assert(res.millisatoshis == 100000 * 1000);
}

/* MUST-ACCEPT (owner-specified rail): the fundee's 100000-sat
 * contribution on a zero balance flows through
 * relative_splice_balance_fundee into update_hsmd_with_splice and the
 * hsmd wire message equals a towire rebuild with
 * push_value = 100_000_000 msat. */
static void test_must_accept_hsmd_wire_contract(void)
{
	struct peer *peer = make_splice_peer(0, 100000, REMOTE, 0);
	struct inflight *inflight = talz(tmpctx, struct inflight);
	struct amount_msat push_val;
	u8 *expected;

	inflight->amnt = amount_sat(1100000);

	push_val = relative_splice_balance_fundee(peer, TX_INITIATOR,
						 NULL, 0, 0);
	assert(push_val.millisatoshis == 100000 * 1000);

	captured_hsmd_len = 0;
	update_hsmd_with_splice(peer, inflight, TX_INITIATOR, push_val);
	assert(captured_hsmd_len > 0);

	/* Byte-exact wire contract: the hsmd message equals a towire
	 * rebuild with push_value = the msat-converted contribution. */
	expected = towire_hsmd_setup_channel(
		NULL,
		peer->channel->opener == LOCAL,
		inflight->amnt,
		amount_msat(100000 * 1000),
		&inflight->outpoint.txid,
		inflight->outpoint.n,
		peer->channel->config[LOCAL].to_self_delay,
		NULL,
		NULL,
		&peer->channel->basepoints[REMOTE],
		&peer->channel->funding_pubkey[REMOTE],
		peer->channel->config[REMOTE].to_self_delay,
		NULL,
		peer->channel->type);
	assert(tal_count(expected) == captured_hsmd_len);
	assert(memcmp(expected, captured_hsmd_msg,
		      captured_hsmd_len) == 0);
	tal_free(expected);
}

/* MUST-ACCEPT (carried balance + precise withdrawal): owed + signed
 * contribution lands exactly - the fundee-with-balance shape the strict
 * signer refused pre-fix, and the negative-relative shape the interim
 * refusal-based fix broke. */
static void test_must_accept_balance_arithmetic(void)
{
	struct amount_msat res;

	/* Fundee carries 500k sat, contributes +100k. */
	res = relative_splice_balance_fundee(
		make_splice_peer(0, 100000, REMOTE, 500000000),
		TX_INITIATOR, NULL, 0, 0);
	assert(res.millisatoshis == 500000000 + 100000000);

	/* Fundee carries 500k sat, withdraws 100k (negative relative). */
	res = relative_splice_balance_fundee(
		make_splice_peer(0, -100000, REMOTE, 500000000),
		TX_INITIATOR, NULL, 0, 0);
	assert(res.millisatoshis == 500000000 - 100000000);
}

/* MUST-ACCEPT (D1 pending term): the total includes HTLCs pending at
 * setup attributable to the FUNDEE and excludes the other side's —
 * the signer allowance must cover the fundee's balance in every HTLC
 * resolution direction (fail -> owed grows back by the escrow). */
static void test_must_accept_pending_htlcs_in_total(void)
{
	struct peer *peer;
	struct amount_msat res;

	/* Fundee LOCAL (channel opener REMOTE, fundee initiates =>
	 * opener_relative): owed 300k sat, fundee-owned pending 75k+25k
	 * sat, OTHER-side pending 400k sat, contribution +100k sat =>
	 * total = 300k + 100k + 100k = 500k sat. */
	peer = make_splice_peer(0, 100000, REMOTE, 300000000);
	add_pending_htlc(peer, 0, amount_msat(75000000), LOCAL);
	add_pending_htlc(peer, 1, amount_msat(25000000), LOCAL);
	add_pending_htlc(peer, 2, amount_msat(400000000), REMOTE);
	res = relative_splice_balance_fundee(peer, TX_INITIATOR, NULL, 0, 0);
	assert(res.millisatoshis == 500000000);

	/* Fundee REMOTE (channel opener LOCAL, we initiate => fundee is
	 * NOT the initiator => accepter_relative): owed 300k sat,
	 * fundee-owned pending 100k sat, other-side pending 400k sat,
	 * contribution -50k sat (a partial withdrawal) =>
	 * total = 300k + 100k - 50k = 350k sat. */
	peer = make_splice_peer(-50000, 0, LOCAL, 300000000);
	add_pending_htlc(peer, 0, amount_msat(100000000), REMOTE);
	add_pending_htlc(peer, 1, amount_msat(400000000), LOCAL);
	res = relative_splice_balance_fundee(peer, TX_INITIATOR, NULL, 0, 0);
	assert(res.millisatoshis == 350000000);
}

/* Refusal harness: the REAL peer_failed chain, forked - child must exit
 * with the status-quit bit set (exit(0x80|reason)) and the warning on
 * the peer pipe must name the refusal. */
static void expect_peer_failed(struct peer *peer, enum tx_role role,
			       const char *expect_substring)
{
	int pfd[2];
	char buf[512];
	size_t off = 0;
	ssize_t n;
	int status;
	pid_t pid;

	assert(pipe(pfd) == 0);
	peer->pps = talz(tmpctx, struct per_peer_state);
	peer->pps->peer_fd = pfd[1];

	pid = fork();
	if (pid == 0) {
		close(pfd[0]);
		relative_splice_balance_fundee(peer, role, NULL, 0, 0);
		_exit(99);
	}
	close(pfd[1]);
	while ((n = read(pfd[0], buf + off, sizeof(buf) - off)) > 0)
		off += n;
	close(pfd[0]);
	buf[off < sizeof(buf) ? off : sizeof(buf) - 1] = '\0';

	assert(waitpid(pid, &status, 0) == pid);
	assert(WIFEXITED(status));
	assert(WEXITSTATUS(status) & 0x80);
	if (memmem(buf, off, expect_substring, strlen(expect_substring)) == NULL) {
		fprintf(stderr, "warning pipe carried %zu bytes without '%s':\n",
			off, expect_substring);
		for (size_t i = 0; i < off; i++)
			fprintf(stderr, "%02x", (unsigned char)buf[i]);
		fprintf(stderr, "\n");
		abort();
	}
}

/* MUST-REFUSE (over-draw): withdrawing beyond the fundee's balance is a
 * peer failure, never a wrapped/garbage push. */
static void test_must_refuse_withdrawal_beyond_balance(void)
{
	expect_peer_failed(make_splice_peer(0, -100000, REMOTE, 0),
			   TX_INITIATOR,
			   "out of range for fundee balance");
}

/* MUST-REFUSE (true negative total): settled + pending + relative
 * below zero is a genuine over-draw — pending HTLCs cushion part of
 * the withdrawal but never mask the over-spend. */
static void test_must_refuse_true_negative_total(void)
{
	/* owed 100k sat + fundee-owned pending 200k sat, withdrawal
	 * -400k sat => total = -100k sat: peer failure, never a
	 * wrapped/garbage push. */
	struct peer *peer = make_splice_peer(0, -400000, REMOTE, 100000000);

	add_pending_htlc(peer, 0, amount_msat(200000000), LOCAL);
	expect_peer_failed(peer, TX_INITIATOR,
			   "out of range for fundee balance");
}

/* MUST-REFUSE (fingerprint): the 1000x fingerprint - a nonzero sat
 * contribution never surfaces as the same number in msat. */
static void test_must_refuse_thousand_x_fingerprint(void)
{
	static const s64 sats[] = { 1, 100, 100000, 10000000 };
	struct amount_msat res;

	for (size_t i = 0; i < ARRAY_SIZE(sats); i++) {
		res = relative_splice_balance_fundee(
			make_splice_peer(0, sats[i], REMOTE, 0),
			TX_INITIATOR, NULL, 0, 0);
		assert(res.millisatoshis == (u64)sats[i] * 1000);
		assert(res.millisatoshis != (u64)sats[i]);
	}
}

/* MUST-REFUSE (overflow): a contribution that overflows the msat add is
 * refused, not truncated. */
static void test_must_refuse_overflow(void)
{
	expect_peer_failed(make_splice_peer(0, INT64_MAX, REMOTE, 0),
			   TX_INITIATOR,
			   "out of range for fundee balance");
}

int main(int argc, char *argv[])
{
	int devnull;

	common_setup(argv[0]);

	/* The real peer_failed chain ends in status_send/status_send_fd;
	 * point status at /dev/null so those paths stay live but quiet. */
	devnull = open("/dev/null", O_WRONLY);
	assert(devnull >= 0);
	status_setup_sync(devnull);

	test_must_accept_conversion();
	test_must_accept_hsmd_wire_contract();
	test_must_accept_balance_arithmetic();
	test_must_accept_pending_htlcs_in_total();
	test_must_refuse_withdrawal_beyond_balance();
	test_must_refuse_true_negative_total();
	test_must_refuse_thousand_x_fingerprint();
	test_must_refuse_overflow();

	common_shutdown();
	return 0;
}

/* AUTOGENERATED MOCKS START */
/* AUTOGENERATED MOCKS END */
