#include "config.h"
#include <common/setup.h>
#include <ccan/compiler/compiler.h>
#include <ccan/tal/tal.h>
#include <common/trace.h>

/* This is mostly a benchmark to see how much overhead the tracing
 * introduces. */

int main(int argx, char *argv[])
{
	/* Just some context objects to hang spans off of. */
	int a = 0, b = 0, c = 0, d = 0;

	common_setup(argv[0]);

	/* Create a bunch of nested spans to emit. */
	for(int i=0; i<2500000; i++) {
		trace_span_start("a", &a);
		trace_span_tag(&a, "method", "getrawblockbyheight");
		trace_span_start("b", &b);
		trace_span_tag(&b, "method", "getrawblockbyheight");

		trace_span_start("c", &c);
		trace_span_tag(&c, "method", "getrawblockbyheight");
		trace_span_end(&c);

		trace_span_end(&b);

		trace_span_start("d", &d);
		trace_span_tag(&d, "method", "getrawblockbyheight");
		trace_span_end(&d);

		trace_span_end(&a);
	}
	/* Orphan-chain regression (CLN#9415): a span suspended with
	 * may_free is force-ended when its key object is freed, so a
	 * later allocation at the recycled address cannot collide. */
	{
		char *obj = tal_arr(NULL, char, 64);
		char *obj2;
		trace_span_start("orphan_test", obj);
		trace_span_suspend_may_free(obj);
		tal_free(obj);

		/* Immediate same-size alloc: the recycled address would
		 * assert on the pre-fix orphan. */
		obj2 = tal_arr(NULL, char, 64);
		trace_span_start("orphan_test_reuse", obj2);
		trace_span_end(obj2);
		tal_free(obj2);
	}

	/* force_end is a no-op when the span already ended normally. */
	{
		char *obj = tal_arr(NULL, char, 64);
		trace_span_start("already_ended", obj);
		trace_span_end(obj);
		trace_span_force_end(obj); /* must not assert or emit */
		tal_free(obj);
	}

	/* force_end on a nested (non-current) span leaves the current
	 * chain alone: exactly the teardown state the bitcoind call
	 * destructor fires in. */
	{
		char *outer = tal_arr(NULL, char, 64);
		char *inner = tal_arr(NULL, char, 64);
		trace_span_start("outer", outer);
		trace_span_start("inner", inner);
		trace_span_suspend_may_free(inner);
		/* inner freed while outer is still current: destructor must
		 * not resume inner into `current` (the old bug) */
		tal_free(inner);
		trace_span_tag(outer, "still_current", "yes");
		trace_span_end(outer);
		tal_free(outer);
	}

	trace_cleanup();
	common_shutdown();
}
