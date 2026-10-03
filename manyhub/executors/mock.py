"""Deterministic, effect-free executor for protocol and recovery tests."""
from ..model import Capabilities, ExecutionRequest, ExecutionResult


class MockExecutor:
    executor_id = "mock"
    capabilities = Capabilities(start_run=True, structured_result=True, resume_owned_run=True)

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.payload.get("mode") == "question" and "reply" not in request.payload:
            return ExecutionResult("awaiting_input", {"question": "Provide a JSON reply"})
        return ExecutionResult("succeeded", {"echo": request.payload})
