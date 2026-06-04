import httpx
import pytest

from app.core.exceptions import (
    NotFoundException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
)
from app.repositories.bastion_mapping_repository import (
    BastionMapping,
    HttpBastionMappingRepository,
)


def _repo(handler) -> HttpBastionMappingRepository:
    transport = httpx.MockTransport(handler)
    return HttpBastionMappingRepository(
        base_url="http://fake",
        token="t",
        timeout_seconds=5,
        transport=transport,
    )


async def test_list_success_returns_mappings_in_order():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/bastion-cluster-mappings"
        assert dict(request.url.params) == {"type": "type1"}
        assert request.headers.get("authorization") == "Bearer t"
        return httpx.Response(
            200,
            json={
                "count": 2,
                "results": [
                    {
                        "pattern": ["type1-cluster-(c1|c2)", "type1-cluster.*"],
                        "runner": "r1",
                        "bastion": "b1",
                        "bastion_ip": "10.0.0.1",
                    },
                    {
                        "pattern": ["type1-kind"],
                        "runner": "r2",
                        "bastion": "b2",
                        "bastion_ip": "10.0.0.2",
                    },
                ],
            },
        )

    repo = _repo(handler)
    result = await repo.list_mappings("type1")
    assert result == [
        BastionMapping(
            pattern=["type1-cluster-(c1|c2)", "type1-cluster.*"],
            runner="r1",
            bastion="b1",
            bastion_ip="10.0.0.1",
        ),
        BastionMapping(
            pattern=["type1-kind"],
            runner="r2",
            bastion="b2",
            bastion_ip="10.0.0.2",
        ),
    ]


async def test_list_empty_results_raises_not_found():
    repo = _repo(lambda r: httpx.Response(200, json={"count": 0, "results": []}))
    with pytest.raises(NotFoundException):
        await repo.list_mappings("unknown")


async def test_list_malformed_payload_raises_upstream_unavailable():
    """200 with missing 'results' key is treated as upstream contract violation."""
    repo = _repo(lambda r: httpx.Response(200, json={"data": []}))
    with pytest.raises(UpstreamUnavailableException):
        await repo.list_mappings("type1")


async def test_list_500_raises_upstream_unavailable():
    repo = _repo(lambda r: httpx.Response(500))
    with pytest.raises(UpstreamUnavailableException):
        await repo.list_mappings("type1")


async def test_list_timeout_raises_upstream_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    repo = _repo(handler)
    with pytest.raises(UpstreamTimeoutException):
        await repo.list_mappings("type1")


async def test_list_connect_error_raises_upstream_unavailable():
    def handler(request):
        raise httpx.ConnectError("nope", request=request)

    repo = _repo(handler)
    with pytest.raises(UpstreamUnavailableException):
        await repo.list_mappings("type1")
