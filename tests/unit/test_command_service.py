import pytest
from app.services.command_service import CommandService, CommandExecutionException

def test_anti_injection_pass():
    svc = CommandService(None)  # repo not used for this method
    svc._validate_anti_injection("safe_string_123")

def test_anti_injection_fail():
    svc = CommandService(None)
    with pytest.raises(CommandExecutionException):
        svc._validate_anti_injection("ls; rm -rf /")
    with pytest.raises(CommandExecutionException):
        svc._validate_anti_injection("$(whoami)")
    with pytest.raises(CommandExecutionException):
        svc._validate_anti_injection("a & b")
    with pytest.raises(CommandExecutionException):
        svc._validate_anti_injection("a | b")
    with pytest.raises(CommandExecutionException):
        svc._validate_anti_injection("`whoami`")
