"""Test package.

Pins the storage backend to `local` BEFORE anything imports `fantasybot.config`,
which reads the environment (and now `.env`) at import time.

This is a safety interlock, not tidiness: without it, running the suite on a
machine configured for production would point every test at the real Supabase
project and write to it. Tests must never be able to reach production, and a
developer should never have to remember to unset something first.
"""

import os

os.environ["FANTASYBOT_STORAGE"] = "local"

