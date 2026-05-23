from app.core.exceptions import ServiceUnavailableException


def test_service_unavailable_exception_attributes():
    exc = ServiceUnavailableException("capacity full")
    assert exc.http_status == 503
    assert exc.error_code == "SERVICE_UNAVAILABLE"
    assert exc.message == "capacity full"
