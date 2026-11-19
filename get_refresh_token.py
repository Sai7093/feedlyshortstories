"""One-time helper to generate a Blogger API v3 OAuth2 refresh token and fetch Blog ID."""

from __future__ import annotations

import argparse
import http.server
import json
import threading
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
BLOG_BY_URL = "https://www.googleapis.com/blogger/v3/blogs/byurl"
SCOPE = "https://www.googleapis.com/auth/blogger"


def load_client_creds(args: argparse.Namespace) -> tuple[str, str]:
    if args.client_secret_file:
        data = json.loads(Path(args.client_secret_file).read_text(encoding="utf-8-sig"))
        info = data.get("installed") or data.get("web") or data
        return info["client_id"].strip(), info["client_secret"].strip()

    client_id = (args.client_id or input("Enter Google OAuth Client ID: ")).strip()
    client_secret = (
        args.client_secret or input("Enter Google OAuth Client Secret: ")
    ).strip()
    return client_id, client_secret


def capture_code_via_local_server(auth_url: str, port: int) -> str:
    captured: dict[str, str] = {}
    done = threading.Event()

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = parse_qs(urlparse(self.path).query)
            if "code" in query:
                captured["code"] = query["code"][0]
                body = (
                    b"<html><body><h2>Authorization complete!</h2>"
                    b"<p>You can close this tab and return to your terminal.</p>"
                    b"</body></html>"
                )
                self.send_response(200)
            else:
                captured["error"] = query.get("error", ["unknown_error"])[0]
                body = b"<html><body><h2>Authorization failed.</h2></body></html>"
                self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", port), CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"\nOpening browser for Google authorization...\nIf it does not open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)
    done.wait(timeout=300)
    server.shutdown()
    server.server_close()

    if "error" in captured:
        raise RuntimeError(f"OAuth authorization error: {captured['error']}")
    if "code" not in captured:
        raise TimeoutError("Timed out waiting for Google OAuth callback.")
    return captured["code"]


def capture_code_manually(auth_url: str) -> str:
    print("\n1. Open this URL in your browser and authorize Blogger access:\n")
    print(auth_url)
    print(
        "\n2. After clicking Allow, your browser will redirect to http://localhost:8765/...\n"
        "   Copy the ENTIRE address bar URL (or just the 'code=...' value) and paste below.\n"
    )
    pasted = input("Paste redirected URL or code: ").strip()
    if pasted.startswith("http"):
        query = parse_qs(urlparse(pasted).query)
        if "code" in query:
            return query["code"][0]
        raise RuntimeError("Could not find 'code=' in the pasted URL.")
    return pasted


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Blogger API v3 OAuth2 refresh token")
    parser.add_argument(
        "--client-secret-file",
        default=None,
        help="Path to downloaded Google Desktop OAuth JSON file",
    )
    parser.add_argument("--client-id", default=None, help="OAuth Client ID")
    parser.add_argument("--client-secret", default=None, help="OAuth Client Secret")
    parser.add_argument(
        "--blog-url",
        default="https://feedlyshortstories.blogspot.com/",
        help="Your blog URL to look up its Blogger Blog ID",
    )
    parser.add_argument("--port", type=int, default=8765, help="Local callback port")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Use copy-paste URL flow (ideal for Google Colab)",
    )
    args = parser.parse_args()

    client_id, client_secret = load_client_creds(args)
    redirect_uri = f"http://localhost:{args.port}/callback"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    full_auth_url = f"{AUTH_URL}?{urlencode(params)}"

    if args.manual:
        code = capture_code_manually(full_auth_url)
    else:
        code = capture_code_via_local_server(full_auth_url, args.port)

    token_resp = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    if token_resp.status_code != 200:
        print(f"Token exchange failed ({token_resp.status_code}): {token_resp.text}")
        return 1

    token_data = token_resp.json()
    refresh_token = token_data.get("refresh_token", "")
    access_token = token_data.get("access_token", "")

    if not refresh_token:
        print(
            "Warning: No refresh_token returned. Revoke app access at "
            "https://myaccount.google.com/permissions and re-run."
        )
        return 1

    blog_id = ""
    if access_token and args.blog_url:
        blog_resp = requests.get(
            BLOG_BY_URL,
            params={"url": args.blog_url},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        if blog_resp.status_code == 200:
            blog_id = str(blog_resp.json().get("id", ""))

    print("\n=== Save these 4 GitHub Repository Secrets ===")
    print(f"BLOGGER_CLIENT_ID     = {client_id}")
    print(f"BLOGGER_CLIENT_SECRET = {client_secret}")
    print(f"BLOGGER_REFRESH_TOKEN = {refresh_token}")
    print(f"BLOGGER_BLOG_ID       = {blog_id or '(could not auto-detect; copy from Blogger URL)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
