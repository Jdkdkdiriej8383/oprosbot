"""Unit tests for the health check web server."""

import pytest
import aiohttp
from utils.health_server import start_health_server


@pytest.mark.asyncio
async def test_health_server_disabled_without_port():
    runner = await start_health_server(port=None)
    assert runner is None


@pytest.mark.asyncio
async def test_health_server_running_and_responding():
    test_port = 18080
    runner = await start_health_server(port=test_port)
    assert runner is not None

    try:
        async with aiohttp.ClientSession() as session:
            # Test root endpoint
            async with session.get(f"http://127.0.0.1:{test_port}/") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "ok"
                assert data["service"] == "oprosbot"

            # Test /health endpoint
            async with session.get(f"http://127.0.0.1:{test_port}/health") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "ok"
    finally:
        await runner.cleanup()
