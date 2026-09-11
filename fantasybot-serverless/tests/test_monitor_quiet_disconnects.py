"""Regression: a closed browser tab is not an error worth printing.

The panel is watched from a console window. Every time a tab was closed while a
request was in flight, the socket error surfaced as a full traceback and read
like the bot had crashed. Silencing the disconnect family keeps that window
trustworthy — a traceback there must always mean something is actually wrong.
"""

import http.server
import unittest
from unittest import mock

from fantasybot import monitor


class ClientDisconnectsAreSilent(unittest.TestCase):

    def _handled_by_the_parent(self, exc):
        """True if the default (printing) handler was reached for `exc`."""
        srv = object.__new__(monitor._Server)     # no socket, no bind
        printed = []
        with mock.patch.object(http.server.ThreadingHTTPServer, "handle_error",
                               lambda self, request, client_address: printed.append(1)):
            try:
                raise exc
            except type(exc):
                srv.handle_error(None, ("127.0.0.1", 51234))
        return bool(printed)

    def test_a_closed_tab_prints_nothing(self):
        for exc in (ConnectionResetError(), ConnectionAbortedError(),
                    BrokenPipeError(), TimeoutError()):
            with self.subTest(exc=type(exc).__name__):
                self.assertFalse(self._handled_by_the_parent(exc))

    def test_a_real_bug_still_prints(self):
        self.assertTrue(self._handled_by_the_parent(ValueError("a real bug")))


if __name__ == "__main__":
    unittest.main()
