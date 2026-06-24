from app.domain.command import CommandStatus
from app.services.command_service import CommandService


class _Repo:
    def __init__(self):
        self.called_with = "unset"

    async def list_states(self, statuses=None):
        self.called_with = statuses
        return []


async def test_default_uses_running_and_killing():
    repo = _Repo()
    svc = CommandService(repo)
    await svc.list_running_commands()
    assert repo.called_with == {CommandStatus.RUNNING, CommandStatus.KILLING}


async def test_explicit_statuses_passed_through():
    repo = _Repo()
    svc = CommandService(repo)
    await svc.list_running_commands({CommandStatus.SUCCESS})
    assert repo.called_with == {CommandStatus.SUCCESS}
