#!/usr/bin/env python3
"""Report which pyln this plugin resolved.

Plugins with a `#!/usr/bin/env python3` shebang are executed by the
system python, which may not provide pyln at all (under a virtualenv
test run) or may provide a different installation. Reporting the
resolved path lets the suite pin that its test plugins import the
tree's own pyln.
"""
import os

from pyln.client import Plugin

plugin = Plugin()


@plugin.method("getpylnpath")
def getpylnpath(plugin):
    import pyln.client
    return {"path": os.path.abspath(pyln.client.__file__)}


plugin.run()
