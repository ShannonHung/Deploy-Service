import pytest
from app.services.command_service import CommandService, CommandExecutionException

def test_anti_injection_pass():
    CommandService._validate_anti_injection("safe_string_123")

def test_anti_injection_fail():
    with pytest.raises(CommandExecutionException):
        CommandService._validate_anti_injection("ls; rm -rf /")
    with pytest.raises(CommandExecutionException):
        CommandService._validate_anti_injection("$(whoami)")
    with pytest.raises(CommandExecutionException):
        CommandService._validate_anti_injection("a & b")
    with pytest.raises(CommandExecutionException):
        CommandService._validate_anti_injection("a | b")
    with pytest.raises(CommandExecutionException):
        CommandService._validate_anti_injection("`whoami`")
