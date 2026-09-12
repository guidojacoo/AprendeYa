"""Names in config.py must not shadow each other.

`config.py` is a flat module of module-level constants, and the serverless
settings were appended to the end of it. That layout has one sharp edge: a second
assignment to an existing name silently wins, with no error anywhere.

It bit exactly once, and expensively. A storage-namespace constant was called
SCOPE, which was already the OAuth scope. `openid offline_access` became
`default`, so LaLiga's authorize URL 404'd — and had it not, the token response
would have carried no refresh_token, because `offline_access` is what grants it.
The bot would have logged itself out after 24 hours with nothing obviously wrong.

These tests pin the values that must never be quietly reassigned.
"""

import unittest

from fantasybot import auth, config


class OAuthConstants(unittest.TestCase):
    def test_scope_requests_offline_access(self):
        """Without offline_access there is no refresh_token, and the 90-day
        session becomes a 24-hour one."""
        self.assertIn("offline_access", config.SCOPE)
        self.assertIn("openid", config.SCOPE)

    def test_scope_is_not_the_storage_namespace(self):
        self.assertNotEqual(config.SCOPE, config.STORAGE_SCOPE)
        self.assertEqual(config.STORAGE_SCOPE, "default")

    def test_the_authorize_url_carries_the_real_scope(self):
        """The end-to-end check: whatever the constants say, this is the URL a
        user actually opens in their browser."""
        url = auth.build_authorize_url("challenge", "state")
        self.assertIn("scope=openid+offline_access", url)
        self.assertNotIn("scope=default", url)

    def test_the_authorize_url_is_otherwise_well_formed(self):
        url = auth.build_authorize_url("challenge", "state")
        for expected in (f"p={config.SIGNIN_POLICY}",
                         f"client_id={config.CLIENT_ID}",
                         "response_type=code",
                         "code_challenge=challenge",
                         "code_challenge_method=S256"):
            self.assertIn(expected, url, f"missing {expected}")
        self.assertTrue(url.startswith(config.AUTHORIZE_ENDPOINT))


class NoDuplicateAssignments(unittest.TestCase):
    def test_no_constant_is_silently_overwritten(self):
        """Catches the next collision at the source, not three layers away in a
        404 from someone else's OAuth server.

        Re-assigning a name is fine when the new value is built FROM it — that is
        a transformation, and upstream does it deliberately for ROOT
        (`ROOT = ... ; ROOT = expanduser(ROOT)`). What must never happen is a
        second, unrelated value taking over the name.
        """
        import ast
        import pathlib

        src = pathlib.Path(config.__file__).read_text(encoding="utf-8")
        seen, shadowed = set(), []
        for node in ast.parse(src).body:
            if not isinstance(node, ast.Assign):
                continue
            refers_to_itself = {n.id for n in ast.walk(node.value)
                                if isinstance(n, ast.Name)}
            for target in node.targets:
                if not isinstance(target, ast.Name) or not target.id.isupper():
                    continue
                if target.id in seen and target.id not in refers_to_itself:
                    shadowed.append(target.id)
                seen.add(target.id)
        self.assertEqual(shadowed, [],
                         f"config.py silently overwrites: {shadowed}")
