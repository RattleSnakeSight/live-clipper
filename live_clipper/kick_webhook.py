"""Isolated, webhook-only HTTP listener for Kick (default 127.0.0.1:8788, setting `monitor.kick_webhook_port`; 0 disables).

Serves exactly one route: POST /webhooks/kick. No dashboard, API, static files, auth, health or docs, so it can be
exposed publicly (the user manages that port's visibility, e.g. through a tunnel) without exposing anything else. Each delivery is
verified with Kick's RSA signature and de-duplicated by message id inside KickChat.handle_webhook.
"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

MAX_BODY = 256 * 1024


def create_webhook_app(engine) -> Starlette:
    async def kick(request: Request) -> Response:
        body = await request.body()
        if len(body) > MAX_BODY:
            return Response(status_code=413)
        status = 404                                   # no armed Kick channel/test -> nothing to deliver to
        targets = [m.chat for m in list(engine.monitors.values())]
        probe = engine.probes.get("kick")
        targets.append(probe.chat if probe else None)
        for chat in targets:
            if chat is not None and getattr(chat, "platform", "") == "kick" and hasattr(chat, "handle_webhook"):
                code = await chat.handle_webhook(dict(request.headers), body)
                if probe and chat is probe.chat and code != 200:
                    probe.rejected_deliveries += 1
                status = min(status, code)
        return Response(status_code=200 if status == 200 else status)

    return Starlette(routes=[Route("/webhooks/kick", kick, methods=["POST"])])
