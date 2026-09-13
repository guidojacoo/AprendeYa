"""Shared scaffolding for the serverless tests.

Every test that touches state runs against a LocalStorage pointed at a fresh temp
directory. That is not a compromise: LocalStorage implements the same contract as
the Supabase backend, so a claim/lease/idempotency test here is a test of the
rules the whole system relies on, runnable with no database and no network.

What it cannot prove is PostgreSQL's own atomicity — so the guard that has to
hold even when the database is WRONG (re-reading the listing before bidding) is
tested separately, and independently of any backend.
"""

import shutil
import tempfile
import unittest

from fantasybot import state as state_mod
from fantasybot import storage
from fantasybot.storage import local as local_mod
from fantasybot.storage.local import LocalStorage

# `state.py` addresses its documents by legacy path constants, and the local
# backend writes to exactly the path it is handed. Repointing only the backend's
# STATE_DIR therefore left state.add_task() writing into the repository's real
# .state/ — polluting the working tree and leaking tasks between tests.
_STATE_PATHS = ("SNAPSHOT_PATH", "TASKS_PATH", "REMINDERS_PATH", "BIDS_PATH",
                "BID_PLAN_PATH", "RIVALS_SNAPSHOT_PATH", "ACTIVITY_HISTORY_PATH",
                "SQUAD_HISTORY_PATH", "PLAYERS_CACHE_PATH")


class StorageTestCase(unittest.TestCase):
    """Gives each test its own empty state directory and a clean backend.

    Its own attributes are prefixed (`_saved_local`, `_saved_state`) because a
    subclass saving its own flags under a plain `self._saved` silently clobbered
    them — and the damage surfaced in teardown, as an unpacking error miles from
    the test that caused it. Twice.
    """

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="fantasybot-test-")
        self._saved_local = (local_mod.STATE_DIR, local_mod.CACHE_DIR,
                       local_mod.EVENTS_PATH, dict(local_mod._SPECIAL_PATHS))
        local_mod.STATE_DIR = f"{self.tmp}/.state"
        local_mod.CACHE_DIR = f"{self.tmp}/.cache"
        local_mod.EVENTS_PATH = f"{self.tmp}/.state/events.jsonl"
        local_mod._SPECIAL_PATHS = {
            "tokens": f"{self.tmp}/tokens.json",
            "pkce": f"{self.tmp}/.pkce.json",
            "run_current": f"{self.tmp}/.state/run.current",
        }
        self._saved_state = {n: getattr(state_mod, n) for n in _STATE_PATHS}
        self._saved_state["STATE_DIR"] = state_mod.STATE_DIR
        self._saved_state["VALUE_HISTORY_DIR"] = state_mod.VALUE_HISTORY_DIR
        state_mod.STATE_DIR = local_mod.STATE_DIR
        state_mod.VALUE_HISTORY_DIR = f"{local_mod.STATE_DIR}/value_history"
        for name in _STATE_PATHS:
            leaf = getattr(state_mod, name).replace("\\", "/").rsplit("/", 1)[-1]
            setattr(state_mod, name, f"{local_mod.STATE_DIR}/{leaf}")

        self.store = storage.set_storage(LocalStorage())
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self._saved_state.items():
            setattr(state_mod, name, value)
        (local_mod.STATE_DIR, local_mod.CACHE_DIR,
         local_mod.EVENTS_PATH, local_mod._SPECIAL_PATHS) = self._saved_local
        storage.reset_storage()
        shutil.rmtree(self.tmp, ignore_errors=True)


class FakeClient:
    """A LaLiga client that records every write, so a test can assert a bid was
    placed exactly once rather than merely that no exception was raised."""

    def __init__(self, market=None):
        self._market = market or []
        self.bids = []
        self.modified = []
        self.market_calls = 0

    def market(self, league_id):
        self.market_calls += 1
        return self._market

    def set_market(self, market):
        self._market = market

    def make_bid(self, league_id, market_id, amount):
        self.bids.append({"league_id": league_id, "market_id": market_id,
                          "amount": amount})
        return {"id": f"bid-{len(self.bids)}"}

    def modify_bid(self, league_id, market_id, bid_id, money):
        self.modified.append({"league_id": league_id, "market_id": market_id,
                              "bid_id": bid_id, "money": money})
        return {"id": bid_id, "money": money}


def listing(market_id="m1", close_iso=None, value=10_000_000, bids=0, mine=None):
    """One market row shaped like LaLiga's, with only the fields we read."""
    row = {"id": market_id, "discr": "marketPlayerLeague",
           "expirationDate": close_iso, "numberOfBids": bids,
           "salePrice": value,
           "playerMaster": {"id": "p1", "nickname": "Tester",
                            "marketValue": value}}
    if mine:
        row["bid"] = mine
    return row
