"""Test package.

Pins the storage backend to `local` BEFORE anything imports `fantasybot.config`,
which reads the environment (and now `.env`) at import time.

This is a safety interlock, not tidiness: without it, running the suite on a
machine configured for production would point every test at the real Supabase
project and write to it. Tests must never be able to reach production, and a
developer should never have to remember to unset something first.
"""

import atexit
import os
import shutil
import tempfile

os.environ["FANTASYBOT_STORAGE"] = "local"

# And point the whole of `.state/` and `.cache/` at a scratch directory, for the
# same reason. Pinning only the BACKEND left every path still rooted in the
# repository, so a test that went near the cache wrote into the working tree and
# the next run read it back: one suite poisoned the next, and the failure landed
# in a test that had nothing to do with the one that caused it.
#
# `config.ROOT` reads FANTASYBOT_HOME at import time, and every path is derived
# from it, so one variable set before the first import isolates all of them.
_HOME = tempfile.mkdtemp(prefix="fantasybot-tests-")
os.environ.setdefault("FANTASYBOT_HOME", _HOME)
# A fresh directory per run is the isolation; removing it afterwards is just not
# leaving one behind on every invocation. A fixed path would be tidier and would
# bring back exactly the cross-run pollution this exists to stop.
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)



# And no test reaches the network. The suite had been scraping futbolfantasy for
# real — `lineup.optimize()` fetches the probable lineups when none are passed —
# and nobody noticed because a cached copy in the repository answered instead.
# Isolating the cache removed the copy and exposed the calls: slow, flaky, and
# dependent on somebody else's website being up.
#
# Blocking it here rather than mocking it per test is deliberate. A test that
# quietly starts depending on the network is the failure being prevented, and it
# can only be prevented somewhere no test can forget about.
import urllib.request  # noqa: E402


def _no_network(*a, **kw):
    raise OSError("tests do not use the network; pass the data in explicitly")


urllib.request.urlopen = _no_network

