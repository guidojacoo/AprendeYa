"""Reading `.env`.

Shipping a `.env.example`, telling people to fill it in, and then never reading
the file is a trap that wastes an afternoon — the variables look set and nothing
uses them. It is also the only way this works on Windows, where `source .env`
does not exist.

The precedence rule is the part worth pinning: the real environment always wins.
On Vercel that environment IS the configuration, and a .env that got bundled into
the deployment must never be able to override it.
"""

import os
import pathlib
import tempfile
import unittest
from unittest import mock

from fantasybot import config


class LoadDotenv(unittest.TestCase):
    def _load(self, text, env=None):
        """Run the loader against `text` as the project's .env."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / ".env"
            path.write_text(text, encoding="utf-8")
            with mock.patch.dict(os.environ, env or {}, clear=True), \
                 mock.patch.object(config, "DOTENV_PATH", str(path)), \
                 mock.patch.object(config, "DOTENV_KEYS", []), \
                 mock.patch.object(config, "DOTENV_FOUND", False):
                config._load_dotenv()
                return dict(os.environ)

    def test_plain_assignment(self):
        got = self._load("SUPABASE_URL=https://x.supabase.co\n")
        self.assertEqual(got["SUPABASE_URL"], "https://x.supabase.co")

    def test_comments_and_blank_lines_are_skipped(self):
        got = self._load("# a comment\n\n  \nKEY=value\n")
        self.assertEqual(got["KEY"], "value")
        self.assertNotIn("# a comment", got)

    def test_surrounding_whitespace_is_trimmed(self):
        got = self._load("  KEY  =  value  \n")
        self.assertEqual(got["KEY"], "value")

    def test_quotes_are_stripped_like_a_shell_would(self):
        got = self._load('A="double"\nB=\'single\'\nC=bare\n')
        self.assertEqual((got["A"], got["B"], got["C"]),
                         ("double", "single", "bare"))

    def test_an_export_prefix_is_tolerated(self):
        got = self._load("export KEY=value\n")
        self.assertEqual(got["KEY"], "value")

    def test_values_may_contain_equals_signs(self):
        """JWTs and base64 keys end in '=' padding — splitting on every '='
        would silently truncate the service_role key."""
        got = self._load("SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOi.payload.sig==\n")
        self.assertEqual(got["SUPABASE_SERVICE_ROLE_KEY"],
                         "eyJhbGciOi.payload.sig==")

    def test_an_empty_value_is_skipped_not_set_to_empty(self):
        """`KEY=` in a template means "not filled in yet". Setting it to an empty
        string makes the variable look configured and moves the failure far away
        from the cause — which is exactly how `FANTASYBOT_STORAGE=supabase` with
        a blank `SUPABASE_URL=` turns into a confusing crash."""
        got = self._load("LLM_API_KEY=\nKEY=value\n")
        self.assertNotIn("LLM_API_KEY", got)
        self.assertEqual(got["KEY"], "value")

    def test_the_real_environment_wins(self):
        got = self._load("SUPABASE_URL=https://from-file.supabase.co\n",
                         env={"SUPABASE_URL": "https://from-vercel.supabase.co"})
        self.assertEqual(got["SUPABASE_URL"], "https://from-vercel.supabase.co",
                         "a bundled .env must never override the real environment")

    def test_lines_without_an_equals_are_ignored(self):
        got = self._load("this is not an assignment\nKEY=value\n")
        self.assertEqual(got["KEY"], "value")

    def test_a_missing_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(pathlib.Path(tmp) / "nope" / ".env")
            with mock.patch.object(config, "DOTENV_PATH", missing), \
                 mock.patch.object(config, "DOTENV_FOUND", False):
                config._load_dotenv()            # must simply do nothing
                self.assertFalse(config.DOTENV_FOUND)

    def test_it_records_that_it_found_the_file(self):
        """The error message a user hits depends on this: "I read this file and
        it set nothing" is a very different problem from "there is no file"."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / ".env"
            path.write_text("SUPABASE_URL=https://x.supabase.co\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(config, "DOTENV_PATH", str(path)), \
                 mock.patch.object(config, "DOTENV_KEYS", []), \
                 mock.patch.object(config, "DOTENV_FOUND", False):
                config._load_dotenv()
                self.assertTrue(config.DOTENV_FOUND)
                self.assertEqual(config.DOTENV_KEYS, ["SUPABASE_URL"])

    def test_a_utf8_bom_does_not_break_the_first_line(self):
        """Windows editors love writing a BOM. Without utf-8-sig the first key
        becomes '\ufeffSUPABASE_URL' and silently never matches."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / ".env"
            path.write_bytes(b"\xef\xbb\xbfSUPABASE_URL=https://x.supabase.co\n")
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(config, "DOTENV_PATH", str(path)), \
                 mock.patch.object(config, "DOTENV_KEYS", []), \
                 mock.patch.object(config, "DOTENV_FOUND", False):
                config._load_dotenv()
                self.assertEqual(os.environ.get("SUPABASE_URL"),
                                 "https://x.supabase.co")


class ProjectUrlNormalisation(unittest.TestCase):
    """Supabase shows both a bare Project URL and a RESTful endpoint with
    `/rest/v1` already appended. Pasting the second one yields
    `/rest/v1/rest/v1/...` and a 404 on every table — which looks exactly like a
    migration that never ran, and sends you debugging the wrong thing."""

    def _rest(self, url):
        from fantasybot.storage.supabase import _project_url
        return f"{_project_url(url)}/rest/v1"

    def test_bare_project_url(self):
        self.assertEqual(self._rest("https://abc.supabase.co"),
                         "https://abc.supabase.co/rest/v1")

    def test_rest_endpoint_is_accepted_too(self):
        self.assertEqual(self._rest("https://abc.supabase.co/rest/v1"),
                         "https://abc.supabase.co/rest/v1")

    def test_trailing_slashes_and_whitespace(self):
        for raw in ("https://abc.supabase.co/", "  https://abc.supabase.co  ",
                    "https://abc.supabase.co/rest/v1/", "https://abc.supabase.co/rest"):
            self.assertEqual(self._rest(raw), "https://abc.supabase.co/rest/v1",
                             f"failed for {raw!r}")

    def test_empty_stays_empty(self):
        from fantasybot.storage.supabase import _project_url
        self.assertEqual(_project_url(""), "")
        self.assertEqual(_project_url(None), "")
