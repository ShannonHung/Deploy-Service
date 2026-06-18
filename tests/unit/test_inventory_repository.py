import httpx
import pytest

from app.core.exceptions import (
    NotFoundException, UpstreamTimeoutException, UpstreamUnavailableException,
)
from app.repositories.inventory_repository import (
    HttpInventoryRepository,
    InventoryBastion,
    InventoryHostInfo,
)


def _client(handler) -> HttpInventoryRepository:
    transport = httpx.MockTransport(handler)
    return HttpInventoryRepository(
        base_url="http://fake",
        token="t",
        timeout_seconds=5,
        transport=transport,
    )


@pytest.mark.asyncio
async def test_lookup_success_returns_info():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/inventory/hosts/node-a01"
        assert request.headers.get("authorization") == "Token t"
        return httpx.Response(
            200,
            json={
                "hostname": "node-a01",
                "ip": "10.0.1.10",
                "bastion": {"hostname": "bastion-a", "ip": "10.0.0.5"},
            },
        )

    repo = _client(handler)
    info = await repo.lookup("node-a01")
    assert info == InventoryHostInfo(
        hostname="node-a01",
        ip="10.0.1.10",
        bastion=InventoryBastion(hostname="bastion-a", ip="10.0.0.5"),
    )


@pytest.mark.asyncio
async def test_lookup_404_raises_not_found():
    repo = _client(lambda r: httpx.Response(404, json={"detail": "nope"}))
    with pytest.raises(NotFoundException):
        await repo.lookup("missing")


@pytest.mark.asyncio
async def test_lookup_401_raises_upstream_unavailable():
    repo = _client(lambda r: httpx.Response(401, json={"detail": "no auth"}))
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup("x")


@pytest.mark.asyncio
async def test_lookup_500_raises_upstream_unavailable():
    repo = _client(lambda r: httpx.Response(500))
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup("x")


@pytest.mark.asyncio
async def test_lookup_timeout_raises_upstream_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)
    repo = _client(handler)
    with pytest.raises(UpstreamTimeoutException):
        await repo.lookup("x")


@pytest.mark.asyncio
async def test_lookup_connect_error_raises_upstream_unavailable():
    def handler(request):
        raise httpx.ConnectError("nope", request=request)
    repo = _client(handler)
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup("x")
