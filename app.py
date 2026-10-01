#!/usr/bin/env python3
"""Internal management API for the Realtime Minecraft server."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from aiohttp import web


API_HOST = "10.77.0.2"
DEFAULT_PORT = 8081
HELPER = "/usr/local/sbin/realtime-minecraft-control"
SUDO = "/usr/bin/sudo"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

LOG = logging.getLogger("realtime_minecraft_api")
TOKEN_KEY = web.AppKey("api_token", str)
RUNNER_KEY = web.AppKey("runner", object)


class CommandFailed(Exception):
    """A known management command did not complete successfully."""


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str


class CommandRunner:
    """Run only the installed, root-owned allowlisting helper."""

    def __init__(self, helper: str = HELPER) -> None:
        self._helper = helper

    async def run(self, action: str, username: str | None = None) -> CommandResult:
        allowed = {"status", "players", "start", "stop", "restart", "whitelist-add", "whitelist-remove"}
        if action not in allowed:
            raise ValueError("unsupported internal action")
        if username is not None and not valid_username(username):
            raise ValueError("invalid username")

        argv = [SUDO, "-n", self._helper, action]
        if username is not None:
            argv.append(username)

        timeout = 120 if action in {"start", "stop", "restart"} else 20
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise CommandFailed("management command timed out") from None

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            LOG.warning("Management helper failed: action=%s exit_code=%s", action, process.returncode)
            raise CommandFailed("management command failed")
        return CommandResult(stdout=stdout, stderr=stderr)


def valid_username(value: Any) -> bool:
    return isinstance(value, str) and USERNAME_RE.fullmatch(value) is not None


def json_error(message: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    if request.path == "/health":
        return await handler(request)

    header = request.headers.get("Authorization", "")
    supplied = header[7:] if header.startswith("Bearer ") else ""
    expected = request.app[TOKEN_KEY]
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        return json_error("unauthorized", 401)
    return await handler(request)


@web.middleware
async def safe_errors(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        return await handler(request)
    except CommandFailed:
        return json_error("management operation failed", 502)
    except web.HTTPException:
        raise
    except Exception:
        LOG.exception("Unhandled request failure on %s", request.path)
        return json_error("internal server error", 500)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "realtime-minecraft-api"})


async def status(request: web.Request) -> web.Response:
    result = await request.app[RUNNER_KEY].run("status")
    try:
        details = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise CommandFailed("invalid helper response") from None
    if not isinstance(details, dict):
        raise CommandFailed("invalid helper response")
    return web.json_response({"ok": True, "minecraft": details})


async def players(request: web.Request) -> web.Response:
    result = await request.app[RUNNER_KEY].run("players")
    return web.json_response({"ok": True, "result": result.stdout})


async def _username_body(request: web.Request) -> str:
    try:
        body = await request.json()
    except (json.JSONDecodeError, web.HTTPBadRequest):
        raise web.HTTPBadRequest(text=json.dumps({"ok": False, "error": "invalid JSON"}), content_type="application/json")
    if not isinstance(body, dict) or not valid_username(body.get("username")):
        raise web.HTTPBadRequest(
            text=json.dumps({"ok": False, "error": "username must be 3-16 letters, digits, or underscores"}),
            content_type="application/json",
        )
    return body["username"]


async def whitelist(request: web.Request) -> web.Response:
    username = await _username_body(request)
    operation = request.match_info["operation"]
    action = "whitelist-add" if operation == "add" else "whitelist-remove"
    result = await request.app[RUNNER_KEY].run(action, username)
    return web.json_response(
        {
            "ok": True,
            "command_succeeded": True,
            "action": f"whitelist/{operation}",
            "username": username,
            "result": result.stdout,
        }
    )


async def lifecycle(request: web.Request) -> web.Response:
    action = request.match_info["action"]
    result = await request.app[RUNNER_KEY].run(action)
    response: dict[str, Any] = {"ok": True, "action": action}
    if result.stdout:
        response["result"] = result.stdout
    return web.json_response(response)


def create_app(*, token: str | None = None, runner: Any | None = None) -> web.Application:
    api_token = token if token is not None else os.environ.get("API_TOKEN", "")
    if len(api_token.encode("utf-8")) < 32:
        raise RuntimeError("API_TOKEN must contain at least 32 bytes")

    app = web.Application(middlewares=[safe_errors, auth_middleware], client_max_size=16 * 1024)
    app[TOKEN_KEY] = api_token
    app[RUNNER_KEY] = runner or CommandRunner()
    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    app.router.add_get("/players", players)
    app.router.add_post("/whitelist/{operation:add|remove}", whitelist)
    app.router.add_post("/server/{action:start|stop|restart}", lifecycle)
    return app


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    configured_host = os.environ.get("API_HOST", API_HOST)
    if configured_host != API_HOST:
        raise RuntimeError(f"API_HOST must be {API_HOST}; refusing to bind elsewhere")
    try:
        port = int(os.environ.get("API_PORT", str(DEFAULT_PORT)))
    except ValueError:
        raise RuntimeError("API_PORT must be an integer") from None
    if not 1 <= port <= 65535:
        raise RuntimeError("API_PORT must be between 1 and 65535")
    web.run_app(create_app(), host=API_HOST, port=port, access_log=None)


if __name__ == "__main__":
    main()
