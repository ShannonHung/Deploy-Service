import httpx
import pytest

from app.core.exceptions import (
    NotFoundException,
    UpstreamTimeoutException,
    UpstreamUnavailableException,
)
from app.repositories.vm_repository import (
    HttpVmRepository,
    VmInfo,
    VmK8sCluster,
)


def _repo(handler) -> HttpVmRepository:
    transport = httpx.MockTransport(handler)
    return HttpVmRepository(
        base_url="http://fake",
        token="t",
        timeout_seconds=5,
        transport=transport,
    )


async def test_lookup_success_returns_vm_info():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/vms"
        assert dict(request.url.params) == {"name": "node1"}
        assert request.headers.get("authorization") == "Bearer t"
        return httpx.Response(
            200,
            json={
                "count": 1,
                "results": [
                    {
                        "id": 123,
                        "name": "node1",
                        "k8s-cluster": {"id": 9, "name": "type1-cluster-c1"},
                    }
                ],
            },
        )

    repo = _repo(handler)
    info = await repo.lookup_by_name("node1")
    assert info == VmInfo(
        id=123,
        name="node1",
        k8s_cluster=VmK8sCluster(id=9, name="type1-cluster-c1"),
    )


async def test_lookup_empty_results_raises_not_found():
    repo = _repo(lambda r: httpx.Response(200, json={"count": 0, "results": []}))
    with pytest.raises(NotFoundException):
        await repo.lookup_by_name("missing")


async def test_lookup_multiple_results_raises_upstream_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 2,
                "results": [
                    {"id": 1, "name": "node1", "k8s-cluster": {"id": 1, "name": "c1"}},
                    {"id": 2, "name": "node1", "k8s-cluster": {"id": 2, "name": "c2"}},
                ],
            },
        )

    repo = _repo(handler)
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup_by_name("node1")


async def test_lookup_500_raises_upstream_unavailable():
    repo = _repo(lambda r: httpx.Response(500))
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup_by_name("x")


async def test_lookup_timeout_raises_upstream_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    repo = _repo(handler)
    with pytest.raises(UpstreamTimeoutException):
        await repo.lookup_by_name("x")


async def test_lookup_connect_error_raises_upstream_unavailable():
    def handler(request):
        raise httpx.ConnectError("nope", request=request)

    repo = _repo(handler)
    with pytest.raises(UpstreamUnavailableException):
        await repo.lookup_by_name("x")
