#include "config.h"
#include <common/setup.h>
#include <ccan/str/str.h>
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
	/* CLN#9415 mechanism, deterministic (campaign #163): a span
	 * suspended and never resumed (the orphan class proven at
	 * shutdown: a plugin call in flight when the plugin is killed)
	 * keeps its key in the table; a NEW span started with the same
	 * key (tal address reuse) is the duplicate-key collision. */
	{
		int x = 0;
		trace_span_start("orphaned-call", &x);
		trace_span_suspend(&x);
		trace_span_start("jsonrpc-cmd", &x);
		trace_span_end(&x);
	}
	trace_cleanup();
	common_shutdown();
}
