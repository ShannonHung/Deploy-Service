from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class CommandArgumentConfig(BaseModel):
    name: str
    type: str  # e.g., "int", "string"
    validation_regex: str = ""

class PipelineStep(BaseModel):
    command: List[str]

class CommandWhitelistConfig(BaseModel):
    command_name: str
    description: str = ""
    disconnects_ssh: bool = False
    killable: bool = False
    pipeline: List[PipelineStep]
    arguments: List[CommandArgumentConfig] = []

class UserCommandWhitelist(BaseModel):
    name: str = "admin"
    allow_commands: List[CommandWhitelistConfig]

class CommandOption(BaseModel):
    timeout_seconds: int = 30

class CommandExecutionRequest(BaseModel):
    command_name: str
    ip_address: str
    ssh_config: str = "default"
    option: Optional[CommandOption] = Field(default_factory=CommandOption)
    arguments: Dict[str, Any] = Field(default_factory=dict)

class CommandExecutionResponse(BaseModel):
    command_id: Optional[str] = None
    status: str
    message: str = ""
    exit_status: Optional[int] = None
    output: Optional[str] = None
