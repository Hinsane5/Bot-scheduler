"""Portal authentication helpers."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

from src import config


ROOT = Path(__file__).resolve().parents[1]
AUTH_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
JWT_VALIDITY_MARGIN = timedelta(minutes=30)

PORTALS: dict[str, dict[str, Any]] = {
    "lms": {
        "mode": "bearer",
        "entry_url": "https://lms.binus.ac.id/lms/dashboard",
        # After login, navigate here to force the schedule API to fire.
        # Templated with current year-month at call time.
        "schedule_url_template": "https://lms.binus.ac.id/lms/schedule/{year}-{month}",
        "api_url_pattern": re.compile(r"func-bm7-.*\.azurewebsites\.net/api/"),
        "header_keys": [
            "Authorization",
            "rOId",
            "academicCareer",
            "institution",
            "roleName",
            "roleId",
        ],
        # If a refresh attempt lands here, SSO cookies are dead.
        "login_url_marker": "login.microsoftonline.com",
    },
    "messier": {
        "mode": "cookie",
        # Land on messier root — the app will redirect to its own login if
        # unauthenticated. The redirect_rewrite handler below keeps the
        # /messier/ prefix intact even when the server drops it.
        "entry_url": "https://socs1.binus.ac.id/messier/",
        "refresh_url": "https://socs1.binus.ac.id/messier/Home.aspx",
        "login_url_marker": "Login.aspx",
        # Pre-set the ASP.NET cookie-support test cookie so the server skips
        # its (broken) detection redirect.
        "preset_cookies": [
            {
                "name": "AspxAutoDetectCookieSupport",
                "value": "1",
                "domain": "socs1.binus.ac.id",
                "path": "/",
            },
        ],
        # Backstop: if the server still tries to redirect us cross-app
        # (drops the /messier/ prefix), rewrite the redirect back into
        # /messier/ at the route layer. Catches /Login.aspx, /Home.aspx,
        # /Default.aspx redirects that lose the prefix.
        "redirect_rewrite": {
            "host": "socs1.binus.ac.id",
            "missing_prefix": "/messier",
            "paths": ["/Login.aspx", "/Home.aspx", "/Default.aspx"],
        },
    },
}


class AuthError(RuntimeError):
    """Raised when auth state is missing or malformed."""


def auth_state_path(portal: str) -> Path:
    _portal_config(portal)
    return ROOT / f"auth_state_{portal}.json"


def load_creds(portal: str) -> dict[str, Any]:
    path = auth_state_path(portal)
    if not path.exists():
        raise AuthError(f"Missing {path.name}; run `python -m src.auth --portal={portal}`.")
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def is_session_valid(portal: str) -> bool:
    try:
        creds = load_creds(portal)
    except (AuthError, json.JSONDecodeError):
        return False

    mode = creds.get("auth_mode") or _portal_config(portal)["mode"]
    if mode == "bearer":
        token_exp = _parse_iso_datetime(creds.get("token_exp"))
        return token_exp is not None and token_exp - _now() > JWT_VALIDITY_MARGIN

    if mode == "cookie":
        last_refresh_at = _parse_iso_datetime(creds.get("last_refresh_at"))
        if last_refresh_at is None:
            return False
        max_age = timedelta(minutes=config.MESSIER_REFRESH_INTERVAL_MIN)
        return _now() - last_refresh_at <= max_age

    return False


async def interactive_login(portal: str) -> dict[str, Any]:
    async_playwright, _ = _load_playwright()
    portal_config = _portal_config(portal)
    mode = portal_config["mode"]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        try:
            # Always use a fresh context for interactive login — stale
            # storage_state from a previous attempt can cause weird redirects
            # (Messier especially).
            context = await browser.new_context()
            preset_cookies = portal_config.get("preset_cookies")
            if preset_cookies:
                await context.add_cookies(preset_cookies)
            rewrite = portal_config.get("redirect_rewrite")
            if rewrite:
                await _install_redirect_rewrite(context, rewrite)
            page = await context.new_page()

            capture: dict[str, Any] = {}
            if mode == "bearer":
                _install_bearer_capture(page, portal_config, capture)

            print(f"\n>>> Opening {portal} login. Complete login in the browser window. <<<", flush=True)
            await page.goto(portal_config["entry_url"], wait_until="domcontentloaded")
            print(">>> Once you reach the post-login page, press Enter here. <<<", flush=True)
            await asyncio.to_thread(input)

            if mode == "bearer":
                # Force the schedule API call by navigating to the schedule page
                # for the current month — the dashboard alone may not fire it.
                schedule_url = _build_schedule_url(portal_config, _now())
                if schedule_url:
                    print(f"Loading schedule page to capture token: {schedule_url}", flush=True)
                    try:
                        await page.goto(schedule_url, wait_until="networkidle", timeout=60_000)
                    except Exception as exc:
                        print(f"Schedule page didn't load cleanly ({exc}); continuing.", flush=True)
                await _wait_for_capture(capture, timeout_sec=60)
                state = await _build_bearer_state(context, capture)
            else:
                state = await _build_cookie_state(context)
        finally:
            await browser.close()

    _write_auth_state(portal, state)
    return state


async def refresh(portal: str) -> bool:
    async_playwright, _ = _load_playwright()
    portal_config = _portal_config(portal)
    mode = portal_config["mode"]
    try:
        load_creds(portal)
    except AuthError:
        return False

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        # Refresh DOES use saved storage_state — we need the SSO/.ASPXAUTH cookies.
        context = await _new_context(browser, portal)
        preset_cookies = portal_config.get("preset_cookies")
        if preset_cookies:
            await context.add_cookies(preset_cookies)
        rewrite = portal_config.get("redirect_rewrite")
        if rewrite:
            await _install_redirect_rewrite(context, rewrite)
        page = await context.new_page()

        if mode == "bearer":
            capture: dict[str, Any] = {}
            _install_bearer_capture(page, portal_config, capture)
            try:
                await page.goto(portal_config["entry_url"], wait_until="domcontentloaded", timeout=30_000)
                # If we bounced to Microsoft login, SSO cookies are dead.
                login_marker = portal_config.get("login_url_marker", "")
                if login_marker and login_marker in page.url:
                    await browser.close()
                    return False
                # Force the schedule API to fire by navigating to the schedule page.
                schedule_url = _build_schedule_url(portal_config, _now())
                if schedule_url:
                    await page.goto(schedule_url, wait_until="networkidle", timeout=30_000)
                await _wait_for_capture(capture, timeout_sec=30)
                state = await _build_bearer_state(context, capture)
            except Exception:
                await browser.close()
                return False
        else:
            try:
                response = await page.goto(portal_config["refresh_url"], wait_until="domcontentloaded")
                final_url = page.url
                content = await page.content()
                login_marker = portal_config["login_url_marker"]
                if login_marker in final_url or login_marker in content:
                    await browser.close()
                    return False
                if response is None or response.status >= 400:
                    await browser.close()
                    return False
                state = await _build_cookie_state(context)
            except Exception:
                await browser.close()
                return False

        await browser.close()

    _write_auth_state(portal, state)
    return True


def _portal_config(portal: str) -> dict[str, Any]:
    try:
        return PORTALS[portal]
    except KeyError as exc:
        choices = ", ".join(sorted(PORTALS))
        raise AuthError(f"Unknown portal {portal!r}; expected one of: {choices}") from exc


def _build_schedule_url(portal_config: dict[str, Any], now: datetime) -> str | None:
    template = portal_config.get("schedule_url_template")
    if not template:
        return None
    return template.format(year=now.year, month=now.month)


async def _install_redirect_rewrite(context: "BrowserContext", config: dict[str, Any]) -> None:
    """Rewrite cross-app redirects that drop the required URL prefix.

    Messier (SOCS) responds to /messier/Login.aspx with a 302 to root
    /Login.aspx?ReturnUrl=/Home.aspx — dropping the /messier/ prefix and
    pointing at non-existent root paths that 404. This handler detects such
    requests at the route layer and rewrites them back under /messier/.
    """
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    host = config["host"]
    missing_prefix = config["missing_prefix"]
    paths = set(config["paths"])

    async def handler(route: Any) -> None:
        request = route.request
        # Only rewrite top-level navigations — leave assets, XHRs, etc. alone.
        if request.resource_type != "document":
            await route.continue_()
            return
        try:
            parsed = urlparse(request.url)
        except Exception:
            await route.continue_()
            return
        if parsed.netloc != host or parsed.path not in paths:
            await route.continue_()
            return
        if parsed.path.startswith(missing_prefix + "/"):
            await route.continue_()
            return

        new_path = missing_prefix + parsed.path
        query_pairs = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key == "ReturnUrl" and value.startswith("/") and not value.startswith(missing_prefix + "/"):
                value = missing_prefix + value
            query_pairs.append((key, value))
        new_url = urlunparse(parsed._replace(path=new_path, query=urlencode(query_pairs)))
        print(f"[redirect-rewrite] {request.url} -> {new_url}", flush=True)
        await route.fulfill(status=302, headers={"Location": new_url}, body="")

    await context.route("**/*", handler)


async def _new_context(browser: Any, portal: str) -> "BrowserContext":
    path = auth_state_path(portal)
    if path.exists():
        try:
            storage_state = load_creds(portal).get("storage_state")
        except (AuthError, json.JSONDecodeError):
            storage_state = None
        if storage_state:
            return await browser.new_context(storage_state=storage_state)
    return await browser.new_context()


def _install_bearer_capture(page: "Page", portal_config: dict[str, Any], capture: dict[str, Any]) -> None:
    pattern = portal_config["api_url_pattern"]
    header_keys = portal_config["header_keys"]

    def on_request(request: Any) -> None:
        if not pattern.search(request.url):
            return

        headers = request.headers
        lower_headers = {key.lower(): value for key, value in headers.items()}
        authorization = lower_headers.get("authorization")
        if not authorization or not authorization.lower().startswith("bearer "):
            return

        body = _parse_post_data(request.post_data)
        if body is None:
            return

        captured_headers: dict[str, str] = {}
        for key in header_keys:
            value = lower_headers.get(key.lower())
            if value is not None:
                captured_headers[key] = value

        print(f"Captured LMS bearer API request: {request.method} {request.url}", flush=True)
        capture.clear()
        capture.update(
            {
                "url": request.url,
                "headers": captured_headers,
                "post_body": body,
            }
        )

    page.on("request", on_request)


async def _wait_for_landing(page: "Page", wait_for_url: str, timeout_ms: int) -> None:
    _, PlaywrightTimeoutError = _load_playwright()
    try:
        await page.wait_for_url(f"{wait_for_url}**", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        if not page.url.startswith(wait_for_url):
            print(f"Did not land on {wait_for_url}; current URL is {page.url}. Continuing.")


async def _wait_for_capture(capture: dict[str, Any], timeout_sec: int) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if capture.get("headers", {}).get("Authorization") and capture.get("post_body") is not None:
            return
        await asyncio.sleep(0.25)
    raise AuthError("Timed out waiting for LMS bearer API request.")


async def _build_bearer_state(context: "BrowserContext", capture: dict[str, Any]) -> dict[str, Any]:
    headers = capture["headers"]
    token = headers["Authorization"].split(" ", 1)[1]
    now = _now_iso()
    return {
        "auth_mode": "bearer",
        "storage_state": await context.storage_state(),
        "bearer_token": token,
        "token_exp": _jwt_exp_iso(token),
        "headers": headers,
        "post_body": capture["post_body"],
        "captured_at": now,
        "last_refresh_at": now,
    }


async def _build_cookie_state(context: "BrowserContext") -> dict[str, Any]:
    now = _now_iso()
    return {
        "auth_mode": "cookie",
        "storage_state": await context.storage_state(),
        "captured_at": now,
        "last_refresh_at": now,
    }


def _parse_post_data(post_data: str | None) -> Any:
    if not post_data:
        return None
    try:
        return json.loads(post_data)
    except json.JSONDecodeError:
        return post_data


def _jwt_exp_iso(token: str) -> str | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    decoded = base64.urlsafe_b64decode(payload + padding)
    claims = json.loads(decoded)
    exp = claims.get("exp")
    if exp is None:
        return None
    return datetime.fromtimestamp(int(exp), tz=timezone.utc).astimezone(config.TZ).isoformat()


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=config.TZ)
    return parsed


def _now() -> datetime:
    return datetime.now(tz=config.TZ)


def _now_iso() -> str:
    return _now().isoformat()


def _write_auth_state(portal: str, state: dict[str, Any]) -> None:
    path = auth_state_path(portal)
    temp_path = path.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
        file.write("\n")
    os.chmod(temp_path, AUTH_FILE_MODE)
    temp_path.replace(path)
    os.chmod(path, AUTH_FILE_MODE)


def _load_playwright() -> tuple[Any, type[Exception]]:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:
        raise AuthError(
            "Playwright is not installed. Run `pip install -r requirements.txt` "
            "and `playwright install chromium` before using auth login/refresh."
        ) from exc
    return async_playwright, PlaywrightTimeoutError


def _print_check(portal: str) -> None:
    print("valid" if is_session_valid(portal) else "invalid")


async def _run_cli(args: argparse.Namespace) -> int:
    if args.check:
        _print_check(args.portal)
        return 0

    if args.refresh:
        ok = await refresh(args.portal)
        print("refreshed" if ok else "refresh_failed")
        return 0 if ok else 1

    await interactive_login(args.portal)
    print(f"Wrote {auth_state_path(args.portal).name}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture or refresh portal auth state.")
    parser.add_argument("--portal", choices=sorted(PORTALS), required=True)
    parser.add_argument("--check", action="store_true", help="Check saved session validity.")
    parser.add_argument("--refresh", action="store_true", help="Refresh saved session headlessly.")
    args = parser.parse_args()

    if args.check and args.refresh:
        parser.error("--check and --refresh are mutually exclusive")

    raise SystemExit(asyncio.run(_run_cli(args)))


if __name__ == "__main__":
    main()
