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
            root = pathlib.Path(tmp) / "project"
            (root / "fantasybot").mkdir(parents=True)
            (root / ".env").write_text(text, encoding="utf-8")
            fake_file = str(root / "fantasybot" / "config.py")
            with mock.patch.dict(os.environ, env or {}, clear=True), \
                 mock.patch.object(config.os.path, "abspath",
                                   side_effect=lambda p: fake_file):
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
            fake = str(pathlib.Path(tmp) / "fantasybot" / "config.py")
            with mock.patch.object(config.os.path, "abspath",
                                   side_effect=lambda p: fake):
                config._load_dotenv()   # must simply do nothing
