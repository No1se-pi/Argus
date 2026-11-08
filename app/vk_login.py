"""Local VK ID login: python -m app.vk_login --client-id YOUR_APP_ID."""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from aiohttp import web
from dotenv import dotenv_values, set_key

REDIRECT_URI = "http://localhost"
TOKEN_URL = "https://id.vk.ru/oauth2/auth"


def authorization_url(client_id: int, verifier: str, state: str, scope: str) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return "https://id.vk.ru/authorize?" + urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "code_challenge": challenge.rstrip(b"=").decode("ascii"),
            "code_challenge_method": "S256",
            "scope": scope,
        }
    )


def parse_callback(query, expected_state: str) -> dict:
    if "payload" in query:
        value = json.loads(query["payload"])
    else:
        value = dict(query)
    if not isinstance(value, dict):
        raise ValueError("Invalid callback")
    returned_state = value.get("state")
    if not isinstance(returned_state, str) or not secrets.compare_digest(
        returned_state, expected_state
    ):
        raise ValueError("State mismatch")
    if value.get("error"):
        raise ValueError("Authorization declined")
    if any(not isinstance(value.get(key), str) or not value[key] for key in ("code", "device_id")):
        raise ValueError("Missing code or device_id")
    return value


async def request_tokens(payload: dict) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.post(TOKEN_URL, data=payload) as response:
            # Never include response bodies or request parameters in error messages.
            if response.status != 200:
                raise ValueError(f"VK ID token exchange failed (HTTP {response.status})")
            result = await response.json()
    if not isinstance(result, dict) or not isinstance(result.get("access_token"), str):
        raise ValueError("VK ID did not issue an access token")
    if not result["access_token"] or result.get("error"):
        raise ValueError("VK ID rejected token exchange")
    return result


def save_tokens(env_path: Path, token_path: Path, result: dict, client_id: int, device_id: str):
    token_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = token_path.with_suffix(".tmp")
    payload = dict(result, client_id=client_id, device_id=device_id)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(payload, stream, ensure_ascii=False)
        temporary.replace(token_path)
    finally:
        temporary.unlink(missing_ok=True)
    set_key(str(env_path), "VK_USER_ACCESS_TOKEN", result["access_token"])


async def run(args) -> int:
    env_path = Path(".env")
    local = dotenv_values(env_path)
    if os.environ.get("VK_USER_ACCESS_TOKEN"):
        print("Remove VK_USER_ACCESS_TOKEN from the process environment first; it overrides .env.")
        return 2
    database = Path(
        os.environ.get("DATABASE_PATH") or local.get("DATABASE_PATH") or "data/argus.sqlite3"
    )
    token_path = database.parent / "vk_id_tokens.json"
    if args.refresh:
        saved = json.loads(token_path.read_text(encoding="utf-8"))
        client_id, device_id = saved["client_id"], saved["device_id"]
        result = await request_tokens(
            {
                "grant_type": "refresh_token",
                "refresh_token": saved["refresh_token"],
                "client_id": client_id,
                "device_id": device_id,
                "state": secrets.token_urlsafe(32),
            }
        )
        if not result.get("refresh_token"):
            result["refresh_token"] = saved["refresh_token"]
    else:
        if not args.client_id or args.client_id <= 0:
            print("Specify --client-id from your own public VK ID application.")
            return 2
        client_id = args.client_id
        verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(32)
        future = asyncio.get_running_loop().create_future()

        async def callback(request):
            headers = {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'",
            }
            if future.done():
                return web.Response(text="Login already received.", status=409, headers=headers)
            try:
                value = parse_callback(request.query, state)
            except (ValueError, TypeError):
                return web.Response(
                    text="Invalid login response. Retry from the VK window.",
                    status=400,
                    headers=headers,
                )
            future.set_result(value)
            return web.Response(
                text="VK response received. Check the Argus console.", headers=headers
            )

        app = web.Application(client_max_size=16384)
        app.router.add_get("/", callback)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            # Loopback only; VK documents localhost port 80 for local HTTP testing.
            await web.TCPSite(runner, "127.0.0.1", 80).start()
            await web.TCPSite(runner, "::1", 80).start()
            url = authorization_url(client_id, verifier, state, args.scope)
            print("Open this URL in a browser on THIS computer (valid for this login only):")
            print(url)
            if not args.no_browser:
                webbrowser.open(url)
            value = await asyncio.wait_for(future, timeout=300)
            device_id = value["device_id"]
            result = await request_tokens(
                {
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": verifier,
                    "code": value["code"],
                    "device_id": device_id,
                    "state": state,
                }
            )
        finally:
            await runner.cleanup()
    save_tokens(env_path, token_path, result, client_id, device_id)
    print("VK_USER_ACCESS_TOKEN saved to .env; token bundle saved under the database directory.")
    print("Restart Argus, then use its VK access check. API method access is not yet verified.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Public VK ID login on http://localhost:80")
    parser.add_argument("--client-id", type=int)
    parser.add_argument(
        "--scope",
        default="vkid.personal_info",
        help="Space-separated permissions enabled for your app in VK ID",
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="Refresh previously saved tokens")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Login cancelled.")
    except (OSError, ValueError, KeyError, aiohttp.ClientError, TimeoutError):
        print("Login failed. Check app settings, localhost port 80 and network; retry the command.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
