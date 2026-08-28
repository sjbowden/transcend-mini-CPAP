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
    2. Create ~/.sleephq_credentials (KEY=VALUE, one per line, chmod 600):
           SLEEPHQ_CLIENT_ID=...
           SLEEPHQ_CLIENT_SECRET=...
           # SLEEPHQ_TEAM_ID=...          (optional; default team is used if omitted)

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
TOKEN_SCOPE = "read write delete"


class SleepHQError(RuntimeError):
    """API call failed (bad credentials, network error, unexpected response)."""


def load_credentials(path):
    creds = {
        "SLEEPHQ_CLIENT_ID": os.environ.get("SLEEPHQ_CLIENT_ID"),
        "SLEEPHQ_CLIENT_SECRET": os.environ.get("SLEEPHQ_CLIENT_SECRET"),
        "SLEEPHQ_TEAM_ID": os.environ.get("SLEEPHQ_TEAM_ID"),
    }
    if os.path.isfile(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key in creds and value.strip():
                    creds[key] = value.strip()
    if not creds["SLEEPHQ_CLIENT_ID"] or not creds["SLEEPHQ_CLIENT_SECRET"]:
        raise SleepHQError(
            f"missing SleepHQ API credentials. Set SLEEPHQ_CLIENT_ID and "
            f"SLEEPHQ_CLIENT_SECRET in {path} (or the environment). Get a "
            f"Client ID/Secret from SleepHQ's Account Settings.")
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


def get_access_token(base_url, client_id, client_secret):
    body = urlencode({
        "grant_type": "password",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": TOKEN_SCOPE,
    }).encode("ascii")
    status, body_bytes = api_request(
        base_url, "POST", "/oauth/token", data=body,
        content_type="application/x-www-form-urlencoded")
    if status != 200:
        raise SleepHQError(f"OAuth failed (HTTP {status}): {body_bytes[:500]!r}")
    return json.loads(body_bytes)["access_token"]


def get_team_id(base_url, token, team_id_override):
    if team_id_override:
        return str(team_id_override)
    status, body_bytes = api_request(base_url, "GET", f"{API_VERSION}/me", token=token)
    if status != 200:
        raise SleepHQError(f"GET /me failed (HTTP {status}): {body_bytes[:500]!r}")
    data = json.loads(body_bytes).get("data", {})
    team_id = data.get("current_team_id") or data.get("attributes", {}).get("current_team_id")
    if not team_id:
        raise SleepHQError(f"could not find current_team_id in /me response: {body_bytes[:500]!r}")
    return str(team_id)


def create_import(base_url, token, team_id):
    status, body_bytes = api_request(
        base_url, "POST", f"{API_VERSION}/teams/{team_id}/imports", token=token)
    if status not in (200, 201):
        raise SleepHQError(f"create import failed (HTTP {status}): {body_bytes[:500]!r}")
    return str(json.loads(body_bytes)["data"]["id"])


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
    token = get_access_token(args.base_url, creds["SLEEPHQ_CLIENT_ID"], creds["SLEEPHQ_CLIENT_SECRET"])
    team_id = get_team_id(args.base_url, token, creds["SLEEPHQ_TEAM_ID"])
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
