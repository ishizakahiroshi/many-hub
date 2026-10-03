"""Product-independent protocol values. Authority is never parsed from task data."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class HubError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Context:
    principal_id: str
    profile_id: str
    scopes: frozenset[str]

    def require(self, scope: str) -> None:
        if scope not in self.scopes and "admin:*" not in self.scopes:
            raise HubError("forbidden", "Required grant is not present")


@dataclass(frozen=True)
class Capabilities:
    start_run: bool = False
    async_result: bool = False
    structured_result: bool = False
    resume_owned_run: bool = False
    cancel_run: bool = False
    interactive_approval: bool = False
    artifacts: bool = False
    threaded_conversation: bool = False


@dataclass(frozen=True)
class ExecutionRequest:
    hub_instance_id: str
    principal_id: str
    profile_id: str
    task_id: str
    request_id: str
    run_id: str
    executor_id: str
    claim_id: str
    payload: dict[str, Any]
    deadline: float
    max_output_bytes: int


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    output: dict[str, Any] = field(default_factory=dict)


class Executor(Protocol):
    executor_id: str
    capabilities: Capabilities

    def execute(self, request: ExecutionRequest) -> ExecutionResult: ...
