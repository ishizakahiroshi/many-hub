"""Safe local defaults; credentials never belong in task payloads or this object."""
from dataclasses import dataclass
from pathlib import Path

from .model import Context


@dataclass(frozen=True)
class Config:
    database: Path = Path("data/manyhub.sqlite3")
    profile_id: str = "local"
    principal_id: str = "local-owner"
    max_pending_tasks: int = 100
    max_requests_per_minute: int = 60

    def local_context(self) -> Context:
        # A local OS user owns the local database. This context is NOT suitable
        # for a remote unauthenticated request; transport adapters must authenticate.
        return Context(self.principal_id, self.profile_id, frozenset({
            "task:create", "task:read", "task:reply", "task:cancel", "executor:list",
            "executor:use:mock", "client:use:local", "transport:use:local",
        }))
