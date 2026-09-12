from __future__ import annotations

import hmac
import ipaddress
from fastapi import Header, HTTPException


class NetworkAndSizeMiddleware:
    def __init__(self, app, allowed_cidrs: tuple[str, ...], max_body_bytes: int = 2 * 1024 * 1024, route_limits=None):
        self.app = app
        self.networks = tuple(ipaddress.ip_network(value, strict=False) for value in allowed_cidrs)
        self.max_body_bytes = max_body_bytes
        self.route_limits = route_limits or {}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        host = scope.get("client", ("", 0))[0]
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            await self._reject(send, 403, b"invalid client address")
            return
        if not any(address in network for network in self.networks):
            await self._reject(send, 403, b"client network refused")
            return
        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        body_limit = self.route_limits.get(scope.get("path"), self.max_body_bytes)
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > body_limit:
                    await self._reject(send, 413, b"request too large")
                    return
            except (ValueError, TypeError):
                await self._reject(send, 400, b"invalid content-length")
                return
        messages, total = [], 0
        while True:
            message = await receive()
            body = message.get("body", b"")
            total += len(body)
            if total > body_limit:
                await self._reject(send, 413, b"request too large")
                return
            messages.append(message)
            if not message.get("more_body", False):
                break
        async def replay():
            return messages.pop(0) if messages else {"type": "http.disconnect"}
        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(send, status: int, body: bytes) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def api_key_dependency(expected: str):
    async def verify(x_pipedal_ai_key: str = Header(default="")) -> None:
        if expected and not hmac.compare_digest(x_pipedal_ai_key, expected):
            raise HTTPException(status_code=401, detail="invalid API key")
    return verify


def bearer_dependency(expected: str):
    async def verify(authorization: str = Header(default="")) -> None:
        supplied = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        if not expected or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid bearer token")
    return verify
