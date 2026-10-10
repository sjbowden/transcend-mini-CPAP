"""Tests for sleephq/upload.py credential loading and the OAuth grant — no
network: api_request is replaced, so nothing leaves the machine.

Covers the two faults that made this uploader unusable on a real account:
the credentials file is JSON (not KEY=VALUE), and SleepHQ's password grant
needs the resource owner's username/password, not just the client pair.

Run:  python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from urllib.parse import parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sleephq"))

import upload  # noqa: E402


def _creds_file(dirpath, payload, mode=0o600, name="creds"):
    p = os.path.join(dirpath, name)
    with open(p, "w") as f:
        f.write(payload if isinstance(payload, str) else json.dumps(payload))
    os.chmod(p, mode)
    return p


FULL = {"client_id": "ci", "client_secret": "cs",
        "username": "u@example.com", "password": "pw"}


class TestCredentialFormats(unittest.TestCase):
    def setUp(self):
        # The environment must not leak into these tests.
        for f in upload.FIELDS:
            os.environ.pop("SLEEPHQ_" + f.upper(), None)

    def test_json_is_the_canonical_format(self):
        # Regression: load_credentials used to parse only KEY=VALUE lines, so a
        # JSON file (what the companion uploader writes) yielded nothing at all
        # and the tool died with "missing credentials" before any network call.
        with tempfile.TemporaryDirectory() as d:
            c = upload.load_credentials(_creds_file(d, FULL))
        self.assertEqual({k: c[k] for k in FULL}, FULL)

    def test_legacy_key_value_still_parses(self):
        body = "".join(f"SLEEPHQ_{k.upper()}={v}\n" for k, v in FULL.items())
        with tempfile.TemporaryDirectory() as d:
            c = upload.load_credentials(_creds_file(d, "# comment\n\n" + body))
        self.assertEqual({k: c[k] for k in FULL}, FULL)

    def test_json_accepts_either_key_spelling_and_non_strings(self):
        payload = dict(FULL)
        payload["SLEEPHQ_CLIENT_ID"] = payload.pop("client_id")
        payload["team_id"] = 42                      # ints must not break it
        with tempfile.TemporaryDirectory() as d:
            c = upload.load_credentials(_creds_file(d, payload))
        self.assertEqual(c["client_id"], "ci")
        self.assertEqual(c["team_id"], "42")

    def test_malformed_json_is_a_clean_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(upload.SleepHQError) as cm:
                upload.load_credentials(_creds_file(d, "{not json"))
        self.assertIn("not valid JSON", str(cm.exception))

    def test_missing_fields_are_named(self):
        with tempfile.TemporaryDirectory() as d:
            p = _creds_file(d, {"client_id": "ci", "client_secret": "cs"})
            with self.assertRaises(upload.SleepHQError) as cm:
                upload.load_credentials(p)
        msg = str(cm.exception)
        self.assertIn("username", msg)
        self.assertIn("password", msg)

    def test_environment_overrides_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = _creds_file(d, FULL)
            os.environ["SLEEPHQ_CLIENT_ID"] = "fromenv"
            self.addCleanup(lambda: os.environ.pop("SLEEPHQ_CLIENT_ID", None))
            self.assertEqual(upload.load_credentials(p)["client_id"], "fromenv")

    def test_absent_file_is_not_an_error_when_the_env_is_complete(self):
        for k, v in FULL.items():
            os.environ["SLEEPHQ_" + k.upper()] = v
            self.addCleanup(lambda k=k: os.environ.pop("SLEEPHQ_" + k.upper(), None))
        c = upload.load_credentials("/nonexistent/sleephq_credentials")
        self.assertEqual({k: c[k] for k in FULL}, FULL)


class TestOAuthGrant(unittest.TestCase):
    def _capture(self):
        sent = {}

        def fake(base_url, method, path, token=None, data=None, headers=None,
                 content_type=None):
            sent["path"] = path
            sent["fields"] = {k: v[0] for k, v in parse_qs(data.decode()).items()}
            return 200, json.dumps({"access_token": "TOK"}).encode()

        orig = upload.api_request
        upload.api_request = fake
        self.addCleanup(lambda: setattr(upload, "api_request", orig))
        return sent

    def test_password_grant_sends_the_resource_owner_credentials(self):
        # Regression: the grant carried only client_id/client_secret, which
        # cannot authenticate a password grant — SleepHQ answers invalid_grant.
        sent = self._capture()
        token = upload.get_access_token("https://sleephq.com", dict(FULL))
        self.assertEqual(token, "TOK")
        self.assertEqual(sent["path"], "/oauth/token")
        self.assertEqual(sent["fields"]["grant_type"], "password")
        self.assertEqual(sent["fields"]["username"], FULL["username"])
        self.assertEqual(sent["fields"]["password"], FULL["password"])
        self.assertEqual(sent["fields"]["client_id"], FULL["client_id"])
        self.assertEqual(sent["fields"]["client_secret"], FULL["client_secret"])

    def test_delete_scope_is_requested(self):
        # Without "delete" the API returns 403 when cleanup tries to remove a
        # half-made import.
        sent = self._capture()
        upload.get_access_token("https://sleephq.com", dict(FULL))
        self.assertIn("delete", sent["fields"]["scope"].split())

    def test_response_without_a_token_raises_cleanly(self):
        def fake(*a, **k):
            return 200, b'{"token_type":"Bearer"}'
        orig = upload.api_request
        upload.api_request = fake
        self.addCleanup(lambda: setattr(upload, "api_request", orig))
        with self.assertRaises(upload.SleepHQError) as cm:
            upload.get_access_token("https://sleephq.com", dict(FULL))
        self.assertIn("no access_token", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
