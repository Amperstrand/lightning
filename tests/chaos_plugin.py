"""The chaos plugin: injects a single dev_disconnect into a splice test.

Reads CHAOS_DISCONNECT (e.g. "-WIRE_TX_COMPLETE") and
CHAOS_DISCONNECT_NODE (1 or 2) from the environment; patches
node_factory's LightningNode constructor to add the disconnect option
to the chosen node (positional-safe via *args/**kwargs pass-through).
Cleanup restores the original constructor.
"""
import os


def pytest_configure(config):
    disc = os.environ.get("CHAOS_DISCONNECT")
    node_num = int(os.environ.get("CHAOS_DISCONNECT_NODE", "1"))
    if not disc:
        return
    from pyln.testing import utils as pyln_utils
    orig_init = pyln_utils.LightningNode.__init__

    def patched_init(self, *args, **kwargs):
        idx = getattr(patched_init, "_count", 0) + 1
        patched_init._count = idx
        if idx == node_num and kwargs.get("disconnect") is None:
            kwargs["disconnect"] = [disc]
        orig_init(self, *args, **kwargs)

    patched_init._count = 0

    def reset_counter(item):
        patched_init._count = 0

    config.pluginmanager.register(
        type("R", (), {
            "pytest_runtest_setup": staticmethod(reset_counter),
        })(), "chaos_reset")
    pyln_utils.LightningNode.__init__ = patched_init
    config.add_cleanup(lambda: setattr(pyln_utils.LightningNode,
                                       "__init__", orig_init))
