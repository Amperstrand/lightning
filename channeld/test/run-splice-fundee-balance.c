/* Unit rails for the splice fundee-balance unit fix (3e03d2aac family,
 * lightning-playground #268): relative_splice_balance_fundee must convert
 * the sat-denominated opener_relative/accepter_relative contributions into
 * the hsmd setup_channel push_value field (amount_msat) exactly - a raw
 * amount_msat() wrap under-reported the fundee's post-splice entitlement
 * 1000x and strict VLS refused the honest first new-era commitment.
 *
 * Rails (SPLICING-AUDIT POLICY, #125 run-sent_commitsigs.c pattern):
 *   MUST-ACCEPT: a 100000-sat contribution reaches the hsmd wire as
 *     push_value = 100_000_000 msat (parsed back out of the captured
 *     towire_hsmd_setup_channel message - the real wiregen, not a mock).
 *   MUST-ACCEPT (withdrawal clamp): a NEGATIVE relative (legitimate
 *     fundee-side withdrawal: RBF rounds that reduce a side, splice-outs)
 *     reports push_value = 0 msat exactly - the pre-fix wrap produced
 *     amount_msat((u64)-1) garbage and the refusal variant broke those
 *     shapes outright (#268 strict ladder, 25 refusals).
 *   MUST-REFUSE (fingerprint): for any nonzero contribution X sat the
 *     reported field is X*1000 msat, never X (the 1000x under-report).
 *   MUST-REFUSE (overflow): a contribution whose msat conversion
 *     overflows peer-fails with "splice funding contribution overflow".
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

static struct peer *make_splicing_peer(const s64 accepter_relative,
				       const s64 opener_relative)
{
	struct peer *peer = talz(tmpctx, struct peer);

	peer->splicing = talz(peer, struct splicing);
	peer->splicing->accepter_relative = accepter_relative;
	peer->splicing->opener_relative = opener_relative;
	return peer;
}

/* MUST-ACCEPT (i)+(ii): exact sat->msat conversion, both roles. */
static void test_must_accept_conversion(void)
{
	struct peer *peer = make_splicing_peer(100000, 100000);
	struct amount_msat res;

	res = relative_splice_balance_fundee(peer, TX_INITIATOR, NULL, 0, 0);
	assert(res.millisatoshis == 100000 * 1000);

	res = relative_splice_balance_fundee(peer, TX_ACCEPTER, NULL, 0, 0);
	assert(res.millisatoshis == 100000 * 1000);
}

/* MUST-ACCEPT (iii): the owner-specified rail - the fundee's 100000-sat
 * contribution flows through relative_splice_balance_fundee into
 * update_hsmd_with_splice and the hsmd wire message parses back with
 * push_value = 100_000_000 msat. */
static void test_must_accept_hsmd_wire_contract(void)
{
	struct peer *peer = make_splicing_peer(100000, 100000);
	struct inflight *inflight = talz(tmpctx, struct inflight);
	struct amount_msat push_val;
	u8 *expected;

	peer->channel = talz(peer, struct channel);
	peer->channel->opener = REMOTE;
	peer->channel->type = channel_type_none_obsolete(peer->channel);
	inflight->amnt = amount_sat(1100000);
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

/* MUST-ACCEPT (withdrawal clamp): a negative relative is a legitimate
 * fundee-side withdrawal; the report is exactly 0 msat (the wrap bug's
 * fingerprint - amount_msat((u64)-x) garbage - must never return). */
static void test_must_accept_negative_clamps_to_zero(void)
{
	static const s64 withdrawals[] = { -1, -100000, -10000000 };
	struct peer *peer = make_splicing_peer(-1, -1);
	struct amount_msat res;

	for (size_t i = 0; i < ARRAY_SIZE(withdrawals); i++) {
		peer->splicing->accepter_relative = withdrawals[i];
		peer->splicing->opener_relative = withdrawals[i];
		res = relative_splice_balance_fundee(peer, TX_INITIATOR,
						    NULL, 0, 0);
		assert(res.millisatoshis == 0);
		res = relative_splice_balance_fundee(peer, TX_ACCEPTER,
						    NULL, 0, 0);
		assert(res.millisatoshis == 0);
	}
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

/* MUST-REFUSE (fingerprint): the 1000x fingerprint - a nonzero sat contribution
 * never surfaces as the same number in msat. */
static void test_must_refuse_thousand_x_fingerprint(void)
{
	static const s64 sats[] = { 1, 100, 100000, 10000000 };
	struct peer *peer = make_splicing_peer(0, 0);
	struct amount_msat res;

	for (size_t i = 0; i < ARRAY_SIZE(sats); i++) {
		peer->splicing->accepter_relative = sats[i];
		res = relative_splice_balance_fundee(peer, TX_INITIATOR,
						    NULL, 0, 0);
		assert(res.millisatoshis == (u64)sats[i] * 1000);
		assert(res.millisatoshis != (u64)sats[i]);
	}
}

/* MUST-REFUSE (overflow): a contribution that cannot convert to msat is
 * refused, not truncated. */
static void test_must_refuse_overflow(void)
{
	expect_peer_failed(make_splicing_peer(INT64_MAX, 0), TX_INITIATOR,
			   "splice funding contribution overflow");
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
	test_must_accept_negative_clamps_to_zero();
	test_must_refuse_thousand_x_fingerprint();
	test_must_refuse_overflow();

	common_shutdown();
	return 0;
}

/* AUTOGENERATED MOCKS START */
/* AUTOGENERATED MOCKS END */
