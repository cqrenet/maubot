# maubot - A plugin-based Matrix bot system.
# Copyright (C) 2022 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from __future__ import annotations

from collections import deque
from datetime import datetime
import asyncio
import logging

from aiohttp import web, web_ws

from mautrix.util import background_task

from .auth import is_valid_token
from .base import routes

BUILTIN_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
}
INCLUDE_ATTRS = {
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "name",
    "pathname",
}
EXCLUDE_ATTRS = BUILTIN_ATTRS - INCLUDE_ATTRS
MAX_LINES = 2048


class LogCollector(logging.Handler):
    lines: deque[dict]
    formatter: logging.Formatter
    listeners: list[web.WebSocketResponse]
    loop: asyncio.AbstractEventLoop

    def __init__(self, level=logging.NOTSET) -> None:
        super().__init__(level)
        self.lines = deque(maxlen=MAX_LINES)
        self.formatter = logging.Formatter()
        self.listeners = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emit(record)
        except Exception as e:
            print("Logging error:", e)

    def _emit(self, record: logging.LogRecord) -> None:
        # JSON conversion based on Marsel Mavletkulov's json-log-formatter (MIT license)
        # https://github.com/marselester/json-log-formatter
        content = {
            name: value for name, value in record.__dict__.items() if name not in EXCLUDE_ATTRS
        }
        content["id"] = str(record.relativeCreated)
        content["msg"] = record.getMessage()
        content["time"] = datetime.fromtimestamp(record.created)

        if record.exc_info:
            content["exc_info"] = self.formatter.formatException(record.exc_info)

        for name, value in content.items():
            if isinstance(value, datetime):
                content[name] = value.astimezone().isoformat()
        asyncio.run_coroutine_threadsafe(self.send(content), loop=self.loop)
        self.lines.append(content)

    async def send(self, record: dict) -> None:
        for ws in self.listeners:
            try:
                await ws.send_json(record)
            except Exception as e:
                print("Log sending error:", e)


handler = LogCollector()
log = logging.getLogger("maubot.server.websocket")
sockets = []


def init(loop: asyncio.AbstractEventLoop) -> None:
    logging.root.addHandler(handler)
    handler.loop = loop


async def stop_all() -> None:
    log.debug("Closing log listener websockets")
    logging.root.removeHandler(handler)
    for socket in sockets:
        try:
            await socket.close(code=1012)
        except Exception:
            pass


@routes.get("/logs")
async def log_websocket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    sockets.append(ws)
    log.debug(f"Connection from {request.remote} opened")
    authenticated = False

    async def close_if_not_authenticated():
        await asyncio.sleep(5)
        if not authenticated:
            await ws.close(code=4000)
            log.debug(f"Connection from {request.remote} terminated due to no authentication")

    background_task.create(close_if_not_authenticated())

    try:
        msg: web_ws.WSMessage
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            if is_valid_token(msg.data):
                await ws.send_json({"auth_success": True})
                await ws.send_json({"history": list(handler.lines)})
                if not authenticated:
                    log.debug(f"Connection from {request.remote} authenticated")
                    handler.listeners.append(ws)
                    authenticated = True
            elif not authenticated:
                await ws.send_json({"auth_success": False})
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass
    if authenticated:
        handler.listeners.remove(ws)
    log.debug(f"Connection from {request.remote} closed")
    sockets.remove(ws)
    return ws
