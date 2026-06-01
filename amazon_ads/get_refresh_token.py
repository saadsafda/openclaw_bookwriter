"""One-time interactive helper to obtain an LWA refresh token.

Usage:
    python -m amazon_ads.get_refresh_token

What it does:
    1. Reads LWA_CLIENT_ID / LWA_CLIENT_SECRET / AMAZON_ADS_OAUTH_REDIRECT_URI
       from your .env.
    2. Starts a tiny local HTTP server on the redirect URI's port.
    3. Opens your browser to Amazon's authorization page.
    4. After you log in and consent, Amazon redirects to /callback with a
       short-lived "code".
    5. We exchange that code for a refresh_token + access_token.
    6. Prints the refresh_token and offers to write it into .env.

The refresh token works across ALL Amazon Ads marketplaces (US/UK/CA/AU/...)
so you only need to do this once.
"""

from __future__ import annotations

import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import requests

from .config import LWA_AUTHORIZE_URL, LWA_SCOPE, LWA_TOKEN_URL, load_config


def _parse_port(redirect_uri: str) -> int:
    parsed = urllib.parse.urlparse(redirect_uri)
    if not parsed.port:
        # Default ports based on scheme
        return 443 if parsed.scheme == "https" else 80
    return parsed.port


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server_version = "AmazonAdsOAuthHelper/1.0"
    received: dict[str, str] = {}

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        query = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.received = {k: v[0] for k, v in query.items()}

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>Amazon authorization received.</h2>"
            "<p>You can close this tab and return to the terminal.</p>"
            "</body></html>"
        )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args, **kwargs):
        # Silence default stderr access log
        pass


def _run_local_server(port: int) -> http.server.HTTPServer:
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _exchange_code(
    code: str, client_id: str, client_secret: str, redirect_uri: str
) -> dict:
    resp = requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Code exchange failed ({resp.status_code}): {resp.text}"
        )
    return resp.json()


def _update_env_file(env_path: Path, refresh_token: str) -> None:
    if not env_path.exists():
        env_path.write_text(f"LWA_REFRESH_TOKEN={refresh_token}\n", encoding="utf-8")
        return

    lines = env_path.read_text(encoding="utf-8").splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.startswith("LWA_REFRESH_TOKEN="):
            lines[i] = f"LWA_REFRESH_TOKEN={refresh_token}"
            found = True
            break
    if not found:
        lines.append(f"LWA_REFRESH_TOKEN={refresh_token}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    cfg = load_config()
    redirect_uri = cfg.redirect_uri
    port = _parse_port(redirect_uri)
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.hostname not in ("localhost", "127.0.0.1"):
        print(
            f"Refusing to bind a public host '{parsed.hostname}'. "
            "Set AMAZON_ADS_OAUTH_REDIRECT_URI to a localhost URL.",
            file=sys.stderr,
        )
        return 2

    state = secrets.token_urlsafe(24)
    auth_url = (
        f"{LWA_AUTHORIZE_URL}?"
        + urllib.parse.urlencode(
            {
                "client_id": cfg.client_id,
                "scope": LWA_SCOPE,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
    )

    server = _run_local_server(port)
    print(f"\nListening on http://127.0.0.1:{port}/callback ...")
    print("Opening your browser to authorize. If it doesn't open, visit:")
    print(f"\n{auth_url}\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    print("Waiting for redirect ... (Ctrl+C to abort)")
    try:
        while not _CallbackHandler.received:
            pass
    except KeyboardInterrupt:
        server.shutdown()
        print("\nAborted.")
        return 1

    server.shutdown()
    received = _CallbackHandler.received

    if received.get("state") != state:
        print("State mismatch - aborting for safety.", file=sys.stderr)
        return 3
    if "error" in received:
        print(
            f"Amazon returned error: {received.get('error')} "
            f"- {received.get('error_description', '')}",
            file=sys.stderr,
        )
        return 4

    code = received.get("code")
    if not code:
        print("No authorization code received.", file=sys.stderr)
        return 5

    print("Exchanging code for refresh token ...")
    tokens = _exchange_code(code, cfg.client_id, cfg.client_secret, redirect_uri)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print(f"No refresh_token in response: {tokens}", file=sys.stderr)
        return 6

    print("\nSUCCESS. Your refresh token:")
    print(f"\n  {refresh_token}\n")

    env_path = Path(__file__).resolve().parent.parent / ".env"
    answer = input(f"Write LWA_REFRESH_TOKEN to {env_path}? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        _update_env_file(env_path, refresh_token)
        print("Wrote LWA_REFRESH_TOKEN to .env")
    else:
        print("Skipped writing .env. Paste the token there manually.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
