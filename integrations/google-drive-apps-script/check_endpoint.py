#!/usr/bin/env python3
"""Send a signed, non-mutating health request to the Rolebot Apps Script."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="Deployed Apps Script /exec URL")
    parser.add_argument("--expected-folder-id")
    args = parser.parse_args()
    secret = os.environ.get("GOOGLE_APPS_SCRIPT_HMAC_SECRET")
    if not secret:
        if not sys.stdin.isatty():
            parser.error("GOOGLE_APPS_SCRIPT_HMAC_SECRET is required in a non-interactive shell")
        secret = getpass.getpass("ROLEBOT_HMAC_SECRET: ")
    if len(secret) < 32:
        parser.error("ROLEBOT_HMAC_SECRET must contain at least 32 characters")

    parsed_url = urllib.parse.urlparse(args.url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.netloc != "script.google.com"
        or re.fullmatch(r"/macros/s/[A-Za-z0-9_-]+/exec", parsed_url.path) is None
        or parsed_url.params
        or parsed_url.query
        or parsed_url.fragment
    ):
        parser.error("url must be an https://script.google.com/macros/s/.../exec URL")

    timestamp = int(time.time())
    request_id = str(uuid.uuid4())
    canonical = "\n".join(["1", str(timestamp), request_id, "health", "", EMPTY_SHA256])
    signature = base64.urlsafe_b64encode(
        hmac.digest(secret.encode("utf-8"), canonical.encode("utf-8"), "sha256")
    ).rstrip(b"=")
    payload = {
        "schema_version": 1,
        "timestamp": timestamp,
        "request_id": request_id,
        "operation": "health",
        "name": "",
        "content_sha256": EMPTY_SHA256,
        "content_base64": "",
        "signature": signature.decode("ascii"),
    }
    request = urllib.request.Request(  # noqa: S310 - URL is restricted above.
        args.url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        parser.error(f"endpoint check failed: {error}")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("ok") is not True:
        parser.error("endpoint returned an error")
    if result.get("request_id") != request_id or result.get("operation") != "health":
        parser.error("endpoint returned a mismatched signed request response")
    if args.expected_folder_id and result.get("folder_id") != args.expected_folder_id:
        parser.error("endpoint returned an unexpected Google Drive folder ID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
