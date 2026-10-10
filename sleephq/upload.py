#!/usr/bin/env python3
"""Upload a ResMed-format SD-card tree (as produced by sleephq/convert.py in
transcend-mini-CPAP) to SleepHQ over its public REST API. Pure stdlib, no
extra dependencies.

SleepHQ API flow (reverse-engineered from SleepHQ's public Swagger docs at
https://sleephq.com/api-docs/index.html and two open-source clients:
https://github.com/twack/sleephq_api_client and the SleepHQUploader in
https://github.com/amanuense/CPAP_data_uploader):

    1. POST /oauth/token                          -> access_token
    2. GET  /api/v1/me                             -> current_team_id
    3. POST /api/v1/teams/{team_id}/imports        -> import_id
    4. POST /api/v1/imports/{import_id}/files      (once per file, multipart)
    5. POST /api/v1/imports/{import_id}/process_files

Setup:
    1. In SleepHQ, go to Account Settings and create an API client to get a
       Client ID and Client Secret.
    2. Create ~/.sleephq_credentials (chmod 600). JSON is the canonical form --
       it is the format the existing community SleepHQ uploaders use, so one
       file can serve several tools:
           {
             "client_id":     "...",
             "client_secret": "...",
             "username":      "you@example.com",
             "password":      "...",
             "team_id":       "..."        (optional; default team if omitted)
           }
       A legacy KEY=VALUE file (SLEEPHQ_CLIENT_ID=... one per line) is also
       accepted. Environment variables SLEEPHQ_CLIENT_ID, SLEEPHQ_CLIENT_SECRET,
       SLEEPHQ_USERNAME, SLEEPHQ_PASSWORD and SLEEPHQ_TEAM_ID override the file.

    The username and password are required: SleepHQ's OAuth endpoint uses a
    *password* grant, so the client pair alone cannot authenticate.

Usage:
    python3 sleephq/upload.py --data-dir ./sleephq/out --all \
        --import-name "Transcend (all, 2026-08-28)"
    python3 sleephq/upload.py --data-dir ./sleephq/out --all --dry-run
"""
import argparse
import hashlib
import json
import mimetypes
import os
import sys
import uuid
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlencode

DEFAULT_BASE_URL = "https://sleephq.com"
DEFAULT_CREDENTIALS = os.path.expanduser("~/.sleephq_credentials")
API_VERSION = "/api/v1"
# "delete" is needed so error-cleanup can remove a half-made import; without
# it the API answers 403 ('requires scope "delete"').
TOKEN_SCOPE = "read write delete"


class SleepHQError(RuntimeError):
    """API call failed (bad credentials, network error, unexpected response)."""


FIELDS = ("client_id", "client_secret", "username", "password", "team_id")
REQUIRED = ("client_id", "client_secret", "username", "password")


def _read_credentials_file(path):
    """Parse the credentials file. JSON is canonical (it is what the companion
    format the existing community SleepHQ uploaders use, so one file can serve
    several tools); the older KEY=VALUE form is still accepted. Returns {} if
    the file is absent."""
    if not os.path.isfile(path):
        return {}
    with open(path) as f:
        text = f.read()

    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError as e:
            raise SleepHQError(f"{path} is not valid JSON: {e}")
        if not isinstance(obj, dict):
            raise SleepHQError(f"{path} must contain a JSON object, got {type(obj).__name__}")
        # Accept either bare keys ("client_id") or the SLEEPHQ_-prefixed spelling.
        out = {}
        for k, v in obj.items():
            key = k.strip().lower()
            if key.startswith("sleephq_"):
                key = key[len("sleephq_"):]
            if key in FIELDS and v:
                out[key] = str(v).strip()
        return out

    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key.startswith("sleephq_"):
            key = key[len("sleephq_"):]
        if key in FIELDS and value.strip():
            out[key] = value.strip()
    return out


def load_credentials(path):
    """Credentials from the environment, falling back to `path`.

    The environment wins: an explicit SLEEPHQ_* variable is a deliberate
    override for one run and should not be silently beaten by the stored file."""
    creds = _read_credentials_file(path)
    for field in FIELDS:
        env = os.environ.get("SLEEPHQ_" + field.upper())
        if env:
            creds[field] = env.strip()

    missing = [f for f in REQUIRED if not creds.get(f)]
    if missing:
        raise SleepHQError(
            f"missing SleepHQ credentials: {', '.join(missing)}. Add them to "
            f"{path} (JSON object with client_id/client_secret/username/password) "
            f"or set SLEEPHQ_{missing[0].upper()} in the environment. The Client "
            f"ID/Secret come from SleepHQ's Account Settings; username/password "
            f"are your SleepHQ login -- the OAuth endpoint uses a password grant, "
            f"so the client pair alone cannot authenticate.")

    if os.path.isfile(path) and (os.stat(path).st_mode & 0o077):
        print(f"warning: {path} is readable by other users; chmod 600 it "
              f"(it holds your SleepHQ password)", file=sys.stderr)
    return creds


def api_request(base_url, method, path, token=None, data=None, headers=None,
                 content_type=None):
    url = base_url + path
    req_headers = {
        "Accept": "application/vnd.api+json",
        # Cloudflare blocks urllib's default "Python-urllib/x.y" UA outright
        # (Error 1010: browser_signature_banned); any normal-looking UA works.
        "User-Agent": "transcend-mini-CPAP-sleephq-uploader/1.0",
    }
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    if content_type:
        req_headers["Content-Type"] = content_type
    if headers:
        req_headers.update(headers)
    req = urlrequest.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urlerror.HTTPError as e:
        return e.code, e.read()
    except (urlerror.URLError, OSError) as e:
        # DNS failure, refused connection, TLS error, socket timeout. Without
        # this a mid-upload network drop exits with a traceback instead of the
        # "error: ..." the __main__ handler prints for every other failure.
        raise SleepHQError(f"{method} {path} failed (network): {e}")


def get_access_token(base_url, creds):
    # A password grant needs the resource owner's credentials, not just the
    # client pair -- without username/password SleepHQ answers invalid_grant.
    body = urlencode({
        "grant_type": "password",
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "username": creds["username"],
        "password": creds["password"],
        "scope": TOKEN_SCOPE,
    }).encode("ascii")
    status, body_bytes = api_request(
        base_url, "POST", "/oauth/token", data=body,
        content_type="application/x-www-form-urlencoded")
    if status != 200:
        raise SleepHQError(f"OAuth failed (HTTP {status}): {body_bytes[:500]!r}")
    try:
        return json.loads(body_bytes)["access_token"]
    except (ValueError, KeyError, TypeError):
        raise SleepHQError(f"no access_token in OAuth response: {body_bytes[:500]!r}")


def get_team_id(base_url, token, team_id_override):
    if team_id_override:
        return str(team_id_override)
    status, body_bytes = api_request(base_url, "GET", f"{API_VERSION}/me", token=token)
    if status != 200:
        raise SleepHQError(f"GET /me failed (HTTP {status}): {body_bytes[:500]!r}")
    try:
        data = json.loads(body_bytes).get("data", {})
        team_id = (data.get("current_team_id")
                   or data.get("attributes", {}).get("current_team_id"))
    except (ValueError, AttributeError):
        team_id = None      # non-JSON, or "data" was a list rather than an object
    if not team_id:
        raise SleepHQError(f"could not find current_team_id in /me response: {body_bytes[:500]!r}")
    return str(team_id)


def create_import(base_url, token, team_id):
    status, body_bytes = api_request(
        base_url, "POST", f"{API_VERSION}/teams/{team_id}/imports", token=token)
    if status not in (200, 201):
        raise SleepHQError(f"create import failed (HTTP {status}): {body_bytes[:500]!r}")
    try:
        return str(json.loads(body_bytes)["data"]["id"])
    except (ValueError, KeyError, TypeError):
        raise SleepHQError(f"no import id in response: {body_bytes[:500]!r}")


def content_hash(abs_path, file_name):
    """SleepHQ's dedup hash: MD5(file content + filename)."""
    h = hashlib.md5()
    with open(abs_path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    h.update(file_name.encode("utf-8"))
    return h.hexdigest()


def upload_file(base_url, token, import_id, abs_path, rel_dir, file_name, log):
    file_hash = content_hash(abs_path, file_name)
    with open(abs_path, "rb") as f:
        file_bytes = f.read()

    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    path_field = "./" + rel_dir if rel_dir else "."

    def field(name, value):
        return (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f'{value}\r\n').encode("utf-8")

    parts = [
        field("name", file_name),
        field("path", path_field),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
         f'filename="{file_name}"\r\nContent-Type: {ctype}\r\n\r\n').encode("utf-8"),
        file_bytes,
        b"\r\n",
        field("content_hash", file_hash),
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(parts)

    status, body_bytes = api_request(
        base_url, "POST", f"{API_VERSION}/imports/{import_id}/files", token=token,
        data=body, content_type=f"multipart/form-data; boundary={boundary}")
    if status not in (200, 201):
        raise SleepHQError(
            f"upload failed for {path_field}/{file_name} (HTTP {status}): {body_bytes[:500]!r}")
    log(f"  uploaded {path_field}/{file_name} ({len(file_bytes)} bytes)")


def process_files(base_url, token, import_id):
    status, body_bytes = api_request(
        base_url, "POST", f"{API_VERSION}/imports/{import_id}/process_files", token=token)
    if status not in (200, 201):
        raise SleepHQError(f"process_files failed (HTTP {status}): {body_bytes[:500]!r}")


def iter_data_files(data_dir):
    for dirpath, _dirnames, filenames in os.walk(data_dir):
        rel_dir = os.path.relpath(dirpath, data_dir)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            yield os.path.join(dirpath, name), rel_dir, name


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True, help="ResMed-format SD tree from convert.py")
    ap.add_argument("--all", action="store_true", help="upload every file under --data-dir")
    ap.add_argument("--import-name", default=None,
                     help="human-readable label for this run (log output only; "
                          "SleepHQ's import API has no name field)")
    ap.add_argument("--credentials", default=DEFAULT_CREDENTIALS)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--dry-run", action="store_true",
                     help="show what would be uploaded; touches no network")
    args = ap.parse_args(argv)

    if not args.all:
        print("error: --all is required (only full-directory upload is supported)", file=sys.stderr)
        return 2
    if not os.path.isdir(args.data_dir):
        print(f"error: --data-dir not found: {args.data_dir}", file=sys.stderr)
        return 2

    files = list(iter_data_files(args.data_dir))
    if not files:
        print(f"error: no files under {args.data_dir}", file=sys.stderr)
        return 2

    label = args.import_name or os.path.basename(os.path.abspath(args.data_dir))
    if args.dry_run:
        print(f"[dry-run] would create import \"{label}\" and upload {len(files)} file(s):")
        for abs_path, rel_dir, name in files:
            print(f"  {('./' + rel_dir) if rel_dir else '.'}/{name}")
        print("[dry-run] nothing sent.")
        return 0

    creds = load_credentials(args.credentials)
    print(f"Authenticating with {args.base_url} ...")
    token = get_access_token(args.base_url, creds)
    team_id = get_team_id(args.base_url, token, creds.get("team_id"))
    print(f"Creating import for team {team_id} (\"{label}\") ...")
    import_id = create_import(args.base_url, token, team_id)
    print(f"Uploading {len(files)} file(s) to import {import_id} ...")
    for abs_path, rel_dir, name in files:
        upload_file(args.base_url, token, import_id, abs_path, rel_dir, name, print)
    print("Requesting processing ...")
    process_files(args.base_url, token, import_id)
    print(f"Done. Import {import_id} submitted for processing.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SleepHQError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
