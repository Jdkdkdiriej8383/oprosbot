"""Lightweight HTTP health check server for cloud hosting (Render, Railway, Fly.io, etc.)."""

import logging
import os
from typing import Optional
from aiohttp import web

logger = logging.getLogger("oprosbot.health")


async def health_handler(request: web.Request) -> web.Response:
    """Returns 200 OK for uptime monitors and container health checks."""
    return web.json_response({
        "status": "ok",
        "service": "oprosbot"
    })


async def start_health_server(port: Optional[int] = None) -> Optional[web.AppRunner]:
    """Starts the health check web server if a port is configured or PORT env is present."""
    if port is None:
        raw_port = os.getenv("PORT")
        if raw_port:
            try:
                port = int(raw_port)
            except ValueError:
                port = None

    if not port or port <= 0:
        logger.info("PORT not set; running in polling-only mode without HTTP health server.")
        return None

    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("HTTP Health check server running on http://0.0.0.0:%d", port)
    return runner
