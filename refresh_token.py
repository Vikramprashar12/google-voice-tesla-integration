"""Refresh the Tesla Fleet API OAuth tokens stored in token.json.

Tesla rotates refresh tokens: every successful refresh returns a NEW
refresh_token and immediately invalidates the one that was sent. That makes
the write below the most dangerous part of this script -- if the new token is
lost after the server has already accepted the request, the old one is dead
too and the only way back is a full manual browser re-authorisation.

So the write is done atomically (temp file -> fsync -> os.replace), and if it
fails anyway the new tokens are dumped to stderr so they can be recovered by
hand rather than lost.
"""

import json
import os
import sys
import tempfile

import requests
from dotenv import load_dotenv

load_dotenv()

# NOTE: .env also defines TESLA_TOKEN_FILE (currently ".tesla_tokens.json"),
# which does not match the file this script actually uses. Left hardcoded on
# purpose so behaviour does not change; reconcile the two when convenient.
TOKEN_FILE = "token.json"

TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"
AUDIENCE = "https://fleet-api.prd.na.vn.cloud.tesla.com"
TIMEOUT = 30


def write_tokens_atomically(path, payload):
    """Write payload to path so a crash can never leave it truncated.

    A plain open(path, "w") truncates immediately, so an interruption between
    truncate and write destroys the only copy of a token Tesla has already
    invalidated. Writing a temp file in the same directory and renaming it over
    the target makes the swap atomic: the file is either fully old or fully new.
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(
        dir=directory, prefix=".token-", suffix=".tmp")
    try:
        # 0600 -- these are bearer credentials, not world-readable data.
        os.chmod(temp_path, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        # Best effort cleanup; the original file is still intact at this point.
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def main():
    try:
        client_id = os.environ["TESLA_CLIENT_ID"]
        client_secret = os.environ["TESLA_CLIENT_SECRET"]
    except KeyError as exc:
        sys.exit(f"Missing {exc.args[0]} -- check your .env file.")

    try:
        with open(TOKEN_FILE) as handle:
            tokens = json.load(handle)
    except FileNotFoundError:
        sys.exit(
            f"{TOKEN_FILE} not found -- run the initial authorisation first.")
    except json.JSONDecodeError as exc:
        sys.exit(
            f"{TOKEN_FILE} is not valid JSON ({exc}); refusing to overwrite it.")

    current_refresh_token = tokens.get("refresh_token")
    if not current_refresh_token:
        sys.exit(f"No 'refresh_token' key in {TOKEN_FILE}; cannot refresh.")

    try:
        response = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": current_refresh_token,
                "audience": AUDIENCE,
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        # Network-level failure: the request never landed, so the existing
        # refresh token is still valid and token.json is untouched.
        sys.exit(f"Request to Tesla failed: {exc}")

    if not response.ok:
        # Tesla puts the useful detail in the body, so surface it. This path is
        # safe: a rejected request means no rotation happened.
        sys.exit(
            f"Tesla returned HTTP {response.status_code}: {response.text.strip()}"
        )

    # Past this point Tesla HAS rotated the token. The old refresh token is now
    # dead, so losing what came back means a full manual re-auth.
    data = response.json()
    new_tokens = {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
    }

    if not new_tokens["access_token"] or not new_tokens["refresh_token"]:
        print("Unexpected response from Tesla -- raw body follows:", file=sys.stderr)
        print(json.dumps(data, indent=2), file=sys.stderr)
        sys.exit(f"Response missing tokens; {TOKEN_FILE} left unchanged.")

    try:
        write_tokens_atomically(TOKEN_FILE, new_tokens)
    except OSError as exc:
        # Last line of defence against a silent lockout: the tokens are live and
        # unsaved, so print them rather than let them evaporate.
        print(f"FAILED to write {TOKEN_FILE}: {exc}", file=sys.stderr)
        print("Save these manually or you will have to re-authorise:", file=sys.stderr)
        print(json.dumps(new_tokens, indent=2), file=sys.stderr)
        raise

    print(f"Token refreshed -> {TOKEN_FILE}")


if __name__ == "__main__":
    main()
