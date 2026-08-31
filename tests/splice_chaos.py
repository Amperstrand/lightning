"""p8 chaos driver: randomized splice-flow interruption sweeps.

Runs CLN's splice tests with a single injected dev_disconnect, chosen
randomly per iteration from the (test, node, prefix, message) grid.
The injection rides a pytest-level env var consumed by a conftest shim
(chaos_conftest); the driver records pass/fail/timeout per iteration.

Usage:
  python3 tests/splice_chaos.py [iterations] [seed]
"""
import os
import random
import subprocess
import sys

TESTS = [
    "tests/test_splicing.py::test_splice",
    "tests/test_splicing.py::test_splice_out",
    "tests/test_splicing.py::test_commit_crash_splice",
    "tests/test_splicing.py::test_splice_rbf",
]

PREFIXES = ["-", "+", "="]
MESSAGES = [
    "WIRE_TX_COMPLETE",
    "WIRE_COMMITMENT_SIGNED",
    "WIRE_TX_SIGNATURES",
    "WIRE_SPLICE_LOCKED",
    "WIRE_COMMITMENT_SIGNED*2",
    "WIRE_TX_COMPLETE*2",
]


def main():
    iters = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    rng = random.Random(seed)
    tree = os.path.expanduser("~/src/vls-splice/lightning")
    env = dict(os.environ)
    env.update({
        "VALGRIND": "0", "DEVELOPER": "1", "PYTHONUNBUFFERED": "1",
        "SUBDAEMON": "hsmd:/tmp/p2inst/vls-proxy-wrapper-counter.sh",
        # The harness env (env.sh semantics) — the wrapper needs the
        # BINARY's own version string (env.sh derives it the same way).
        "VLS_CLN_VERSION": subprocess.run(
            [os.path.join(tree, "lightningd/lightningd"), "--version"],
            capture_output=True, text=True).stdout.strip(),
        "VLS_NETWORK": "regtest",
        "VLS_PERMISSIVE": "1",
        "BITCOIND_RPC_URL": "http://user:pass@127.0.0.1:18443",
        "TIMEOUT": "180",
    })
    results = {"PASS": 0, "FAIL": 0, "TIMEOUT": 0}
    fails = []
    for i in range(iters):
        test = rng.choice(TESTS)
        disc = rng.choice(PREFIXES) + rng.choice(MESSAGES)
        node = rng.choice([1, 2])
        env["CHAOS_DISCONNECT"] = disc
        env["CHAOS_DISCONNECT_NODE"] = str(node)
        tag = f"[{i+1}/{iters}] {test.split('::')[-1]} n{node} {disc}"
        try:
            r = subprocess.run(
                [os.path.join(tree, ".venv/bin/python"), "-m", "pytest",
                 test, "-q", "-x", "--timeout=200",
                 "-p", "tests.chaos_plugin"],
                env=env, cwd=tree, capture_output=True, text=True, timeout=300,
            )
            out = r.stdout + r.stderr
            summary = [l for l in out.splitlines()
                       if " passed" in l or " failed" in l]
            summary_line = summary[-1] if summary else ""
            if r.returncode == 0 or "1 passed" in summary_line:
                results["PASS"] += 1
                print(f"{tag}: PASS", flush=True)
            else:
                why = summary[-1] if summary else out.strip()[-150:]
                results["FAIL"] += 1
                fails.append((tag, why))
                print(f"{tag}: FAIL — {why}", flush=True)
        except subprocess.TimeoutExpired:
            results["TIMEOUT"] += 1
            fails.append((tag, "HARD TIMEOUT"))
            print(f"{tag}: TIMEOUT", flush=True)
    print(f"\n=== CHAOS SUMMARY (seed {seed}): PASS={results['PASS']} "
          f"FAIL={results['FAIL']} TIMEOUT={results['TIMEOUT']}", flush=True)
    for tag, why in fails:
        print(f"  {tag}: {why}", flush=True)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
