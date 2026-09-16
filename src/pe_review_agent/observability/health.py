from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

ReadyCheck = Callable[[], Awaitable[tuple[bool, str]]]


class HealthServer:
    def __init__(self, host: str, port: int, ready_check: ReadyCheck | None = None) -> None:
        self.host = host
        self.port = port
        self.ready_check = ready_check
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)

    async def close(self) -> None:
        if not self._server:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            parts = request_line.decode("ascii", errors="ignore").split()
            path = parts[1] if len(parts) >= 2 else "/"
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                if line in (b"\r\n", b"\n", b""):
                    break
            if path == "/healthz":
                await self._send_json(writer, 200, {"status": "ok"})
            elif path == "/readyz":
                ready, detail = (True, "ready")
                if self.ready_check:
                    ready, detail = await self.ready_check()
                await self._send_json(
                    writer,
                    200 if ready else 503,
                    {"status": "ready" if ready else "not_ready", "detail": detail},
                )
            elif path == "/metrics":
                body = generate_latest()
                await self._send(writer, 200, body, CONTENT_TYPE_LATEST)
            else:
                await self._send_json(writer, 404, {"error": "not found"})
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _send_json(
        self, writer: asyncio.StreamWriter, status: int, payload: dict[str, object]
    ) -> None:
        await self._send(
            writer,
            status,
            json.dumps(payload, separators=(",", ":")).encode(),
            "application/json; charset=utf-8",
        )

    async def _send(
        self, writer: asyncio.StreamWriter, status: int, body: bytes, content_type: str
    ) -> None:
        reason = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}.get(status, "OK")
        writer.write(
            (
                f"HTTP/1.1 {status} {reason}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            + body
        )
        await writer.drain()
