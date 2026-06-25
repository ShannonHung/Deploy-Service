from app.services.command_service import _decode


def test_decode_bytes():
    assert _decode(b"hello") == "hello"


def test_decode_str_passthrough():
    assert _decode("hello") == "hello"


def test_decode_none_is_empty():
    assert _decode(None) == ""


def test_decode_empty_bytes_is_empty():
    assert _decode(b"") == ""
