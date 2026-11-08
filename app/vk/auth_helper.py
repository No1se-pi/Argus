"""Short-lived phone access to a server-side VK browser, for explicit admin login."""

import asyncio
import contextlib
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from aiohttp import web

from app.vk.client import VKClient, configured_vk_proxy

logger = logging.getLogger(__name__)
ASSETS = Path(__file__).parent / "auth_ui"
LIFETIME = 600
COOKIE = "argus_vk_login"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def browser_proxy(url: str | None) -> dict | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("VK_PROXY_URL must be an HTTP(S) proxy")
    value = {"server": f"{parsed.scheme}://{parsed.netloc.rsplit('@', 1)[-1]}"}
    if parsed.username is not None:
        value["username"] = unquote(parsed.username)
        value["password"] = unquote(parsed.password or "")
    return value


def website_token(request) -> str | None:
    """Observe only API requests actually made by the VK website in this browser."""
    parsed = urlparse(request.url)
    if parsed.scheme != "https" or parsed.hostname != "web.api.vk.ru":
        return None
    if not parsed.path.startswith("/method/"):
        return None
    values = parse_qs(request.post_data or "")
    value = values.get("access_token", parse_qs(parsed.query).get("access_token", [""]))[0]
    if value.startswith("vk1.a.") and len(value) <= 4096:
        return value
    return None


@dataclass
class Ticket:
    secret_hash: str
    chat_id: int
    group_id: int
    expires: float
    cookie_hash: str | None = None


class VKAuthHelper:
    def __init__(
        self, *, public_url: str, port: int, workspace, notify=None, bind_host: str = "127.0.0.1"
    ):
        parsed = urlparse(public_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("VK_AUTH_PUBLIC_URL must be an HTTPS origin without a path")
        self.public_url = public_url.rstrip("/")
        self.port = port
        self.bind_host = bind_host
        self.workspace = workspace
        self.notify = notify
        self.ticket = None
        self.status = "idle"
        self.context = None
        self.page = None
        self.playwright = None
        self.expiry_task = None
        self.capture_task = None
        self.runner = None
        self.lock = asyncio.Lock()
        self.candidates = asyncio.Queue(maxsize=1)
        self.seen = set()
        self.page_opened_at = None
        self.app = web.Application(client_max_size=8192, middlewares=[self.headers])
        self.app.router.add_get("/vk-connect", self.index)
        self.app.router.add_get("/vk-connect/app.js", self.asset)
        self.app.router.add_get("/vk-connect/style.css", self.asset)
        self.app.router.add_get("/vk-connect/frame", self.frame)
        self.app.router.add_post("/vk-connect/action", self.action)

    @web.middleware
    async def headers(self, request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.Response(status=exc.status, text=exc.text, headers=exc.headers)
        except Exception:
            # Browser and network exceptions may include URLs or input; never log them.
            logger.warning("VK login helper request failed")
            response = web.json_response(
                {"error": "Не удалось выполнить действие. Повторите вход."}, status=503
            )
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; "
                "style-src 'self'; "
                "img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            }
        )
        return response

    async def start(self):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.bind_host, self.port).start()

    async def issue(self, chat_id: int, group_id: int) -> str:
        async with self.lock:
            if (
                self.ticket
                and self.ticket.chat_id != chat_id
                and time.monotonic() < self.ticket.expires
            ):
                raise ValueError("Сейчас другой администратор подключает ВК. Повторите позже.")
            await self.stop_session()
            secret = secrets.token_urlsafe(32)
            self.ticket = Ticket(
                digest(secret), chat_id, abs(group_id), time.monotonic() + LIFETIME
            )
            self.status = "ready"
            self.candidates = asyncio.Queue(maxsize=1)
            self.seen.clear()
            self.expiry_task = asyncio.create_task(self.expire(), name="vk-login-expiry")
            # Fragment is not sent in HTTP URLs or reverse-proxy access logs.
            return f"{self.public_url}/vk-connect#{secret}"

    async def expire(self):
        await asyncio.sleep(LIFETIME)
        async with self.lock:
            await self.stop_session()
            self.ticket = None
            self.status = "expired"

    async def stop_session(self):
        current = asyncio.current_task()
        for task in (self.expiry_task, self.capture_task):
            if task and task is not current:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self.expiry_task = self.capture_task = None
        await self.close_browser()

    async def close_browser(self):
        if self.context:
            with contextlib.suppress(Exception):
                await self.context.close()
        if self.playwright:
            with contextlib.suppress(Exception):
                await self.playwright.stop()
        self.context = self.page = self.playwright = None

    async def close(self):
        async with self.lock:
            await self.stop_session()
            self.ticket = None
        if self.runner:
            await self.runner.cleanup()

    def authenticate(self, request, *, opening=False):
        ticket = self.ticket
        if ticket is None or time.monotonic() >= ticket.expires:
            raise web.HTTPUnauthorized(text="Ссылка истекла. Получите новую в боте.")
        supplied = request.headers.get("X-Argus-Auth", "")
        if not secrets.compare_digest(digest(supplied), ticket.secret_hash):
            raise web.HTTPUnauthorized(text="Недействительная ссылка.")
        origin = request.headers.get("Origin")
        if origin and origin != self.public_url:
            raise web.HTTPForbidden(text="Неверный источник запроса.")
        if ticket.cookie_hash:
            cookie = request.cookies.get(COOKIE, "")
            if not secrets.compare_digest(digest(cookie), ticket.cookie_hash):
                raise web.HTTPForbidden(text="Ссылка уже открыта в другом браузере.")
        elif not opening:
            raise web.HTTPForbidden(text="Сначала откройте окно входа.")

    async def index(self, request):
        return web.Response(body=(ASSETS / "index.html").read_bytes(), content_type="text/html")

    async def asset(self, request):
        name = request.path.rsplit("/", 1)[-1]
        return web.Response(
            body=(ASSETS / name).read_bytes(),
            content_type="text/javascript" if name.endswith(".js") else "text/css",
        )

    async def open_browser(self):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        profile = self.workspace.template_path.parent / "vk_auth_browser"
        profile.mkdir(parents=True, exist_ok=True)
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(profile),
            headless=True,
            viewport={"width": 1000, "height": 760},
            locale="ru-RU",
            accept_downloads=False,
            proxy=browser_proxy(configured_vk_proxy(self.workspace.service.vk_service.settings)),
        )
        self.context.on("request", self.observe)
        self.context.on("page", self.new_page)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.capture_task = asyncio.create_task(self.capture(), name="vk-login-capture")
        self.status = "waiting"
        self.page_opened_at = time.monotonic()
        await self.page.goto(
            "https://vk.ru/login",
            wait_until="domcontentloaded",
            timeout=45000,
        )

    def new_page(self, page):
        self.page = page

    def observe(self, request):
        token = website_token(request)
        if token and token not in self.seen and not self.candidates.full():
            self.seen.add(token)
            self.candidates.put_nowait(token)

    async def verify(self, token):
        client = VKClient(
            access_token=token,
            api_version="5.289",
            web_mode=True,
            proxy=configured_vk_proxy(self.workspace.service.vk_service.settings),
        )
        group = self.ticket.group_id
        wall = await client.request("wall.get", {"owner_id": -group, "count": 1})
        posts = wall.get("items", []) if isinstance(wall, dict) else []
        if not posts:
            raise ValueError("No post for likes access check")
        await asyncio.sleep(0.4)
        await client.request(
            "likes.getList",
            {
                "type": "post",
                "owner_id": -group,
                "item_id": posts[0]["id"],
                "count": 1,
                "friends_only": 0,
                "extended": 0,
            },
        )
        await asyncio.sleep(0.4)
        await client.request("users.get", {"user_ids": "1"})

    async def capture(self):
        while True:
            token = await self.candidates.get()
            self.status = "checking"
            try:
                await self.verify(token)
            except Exception:
                self.status = "rejected"
                continue
            async with self.lock:
                await self.workspace.settings.set("vk_web_access_token", token, is_secret=True)
                chat_id = self.ticket.chat_id
                self.status = "connected"
                await self.close_browser()
            if self.notify:
                with contextlib.suppress(Exception):
                    await self.notify(
                        chat_id,
                        "✅ ВК подключён. Доступ к постам, лайкам и профилям проверен. "
                        "Можно собирать отчёт, перезапуск бота не нужен.",
                    )
            return

    async def frame(self, request):
        async with self.lock:
            self.authenticate(request)
            if not self.page or self.status == "connected":
                raise web.HTTPConflict(text="Окно входа закрыто.")
            image = await self.page.screenshot(type="jpeg", quality=70, timeout=10000)
            return web.Response(body=image, content_type="image/jpeg")

    async def page_view(self) -> str:
        try:
            text = await self.page.locator("body").inner_text(timeout=1500)
            text = text.replace("\u200c", "").replace("\u200b", "").strip()
            if len(text) < 30:
                age = time.monotonic() - (self.page_opened_at or time.monotonic())
                return "stalled" if age > 20 else "loading"
            title = (await self.page.title()).lower()
            if "не робот" in title or "captcha" in title:
                return "challenge"
            return "interactive"
        except Exception:
            return "loading"

    async def action(self, request):
        data = await request.json()
        if not isinstance(data, dict):
            raise web.HTTPBadRequest()
        action = data.get("action")
        async with self.lock:
            self.authenticate(request, opening=action == "open")
            new_cookie = None
            if action == "open":
                if not self.ticket.cookie_hash:
                    new_cookie = secrets.token_urlsafe(32)
                    self.ticket.cookie_hash = digest(new_cookie)
                if not self.context and self.status != "connected":
                    try:
                        await self.open_browser()
                    except Exception:
                        await self.close_browser()
                        self.status = "unavailable"
            elif action == "cancel":
                await self.stop_session()
                self.ticket = None
                return web.json_response({"status": "cancelled"})
            elif action != "status":
                if not self.page or self.status == "connected":
                    raise web.HTTPConflict()
                if action == "click":
                    x, y = float(data.get("x", -1)), float(data.get("y", -1))
                    if not (0 <= x <= 1000 and 0 <= y <= 760):
                        raise web.HTTPBadRequest()
                    await self.page.mouse.click(x, y)
                elif action == "type":
                    text = data.get("text")
                    if not isinstance(text, str) or len(text) > 1024:
                        raise web.HTTPBadRequest()
                    await self.page.keyboard.insert_text(text)
                elif action == "key" and data.get("key") in {"Enter", "Tab", "Backspace", "Escape"}:
                    await self.page.keyboard.press(data["key"])
                elif action == "scroll":
                    await self.page.mouse.wheel(0, 500 if data.get("down", True) else -500)
                elif action == "community":
                    self.page_opened_at = time.monotonic()
                    await self.page.goto(
                        f"https://vk.ru/club{self.ticket.group_id}",
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                elif action == "login":
                    self.page_opened_at = time.monotonic()
                    await self.page.goto(
                        "https://vk.ru/login", wait_until="domcontentloaded", timeout=45000
                    )
                elif action == "reload":
                    self.page_opened_at = time.monotonic()
                    await self.page.reload(wait_until="domcontentloaded", timeout=45000)
                else:
                    raise web.HTTPBadRequest()
            # Never expose query strings/fragments, credentials, cookies or browser exceptions.
            host = urlparse(self.page.url).hostname if self.page else ""
            result = {"status": self.status, "host": host}
            if self.page and self.page_opened_at is not None:
                result["view"] = await self.page_view()
            response = web.json_response(result)
            if new_cookie:
                response.set_cookie(
                    COOKIE,
                    new_cookie,
                    secure=True,
                    httponly=True,
                    samesite="Strict",
                    max_age=LIFETIME,
                    path="/vk-connect",
                )
            return response
