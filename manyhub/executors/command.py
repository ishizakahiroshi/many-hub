"""An operator-allowlisted fixed-argv process adapter, not a hostile-code sandbox.

Task data goes only to bounded JSON stdin. No argument, executable, environment,
working directory, credential, or shell command is selected by the task.
"""
from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..model import Capabilities, ExecutionRequest, ExecutionResult
from ..service import MAX_INPUT_BYTES, bounded_json, identifier


@dataclass(frozen=True)
class CommandSpec:
    executor_id: str
    executable: Path
    arguments: tuple[str, ...]
    workspace: Path
    allowed_executables: tuple[Path, ...]
    timeout_seconds: float = 10
    max_output_bytes: int = 65536


class CommandExecutor:
    """POSIX-only until equivalent Windows process-tree containment is verified."""
    capabilities = Capabilities(start_run=True, structured_result=True)

    def __init__(self, spec: CommandSpec):
        if os.name != "posix":
            raise ValueError("Command Executor requires verified POSIX process-group support")
        identifier(spec.executor_id, "executor_id")
        if not spec.executable.is_absolute() or not spec.workspace.is_absolute():
            raise ValueError("Executable and workspace must be absolute paths")
        executable = spec.executable.resolve(strict=True)
        allowed = {path.resolve(strict=True) for path in spec.allowed_executables if path.is_absolute()}
        workspace = spec.workspace.resolve(strict=True)
        if executable not in allowed or not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("Executable is not allowlisted and executable")
        if not workspace.is_dir():
            raise ValueError("Workspace must be an existing dedicated directory")
        if not isinstance(spec.arguments, tuple) or any(not isinstance(arg, str) or "\x00" in arg for arg in spec.arguments):
            raise ValueError("Arguments must be fixed strings")
        if type(spec.timeout_seconds) not in (int, float) or not math.isfinite(spec.timeout_seconds) or not 0 < spec.timeout_seconds <= 300:
            raise ValueError("Invalid command timeout")
        if type(spec.max_output_bytes) is not int or not 1 <= spec.max_output_bytes <= 65536:
            raise ValueError("Invalid output limit")
        self.spec = spec
        self.executor_id = spec.executor_id
        self.executable = executable
        self.workspace = workspace

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        try:
            # Each child has a new session/group. This never targets Hub's group.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.executor_id != self.executor_id:
            return ExecutionResult("failed", {"error": "executor_mismatch"})
        if request.deadline <= time.time():
            return ExecutionResult("failed", {"error": "expired_before_spawn"})
        payload = bounded_json(request.payload, MAX_INPUT_BYTES).encode("utf-8")
        limit = min(self.spec.max_output_bytes, request.max_output_bytes)
        # Never copy Hub's environment (tokens, cloud keys, proxies, cookies, etc.).
        environment = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        if request.deadline <= time.time():
            return ExecutionResult("failed", {"error": "expired_before_spawn"})
        started_threads = []
        reason = None
        try:
            process = subprocess.Popen([str(self.executable), *self.spec.arguments], cwd=self.workspace,
                                       env=environment, shell=False, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True, close_fds=True)
        except OSError:
            return ExecutionResult("failed", {"error": "spawn_failed"})
        try:
            stdout = bytearray()
            exceeded = threading.Event()
            stream_failed = threading.Event()
            lock = threading.Lock()
            total = 0

            def drain(stream, retain):
                nonlocal total
                try:
                    while chunk := stream.read(4096):
                        with lock:
                            total += len(chunk)
                            if total > limit:
                                exceeded.set()
                            elif retain:
                                stdout.extend(chunk)
                except (OSError, ValueError):
                    stream_failed.set()
                finally:
                    stream.close()

            def write_input():
                try:
                    process.stdin.write(payload + b"\n")
                    process.stdin.flush()
                except (OSError, ValueError):
                    # A process may close stdin; its result remains checked.
                    pass
                finally:
                    try:
                        process.stdin.close()
                    except (OSError, ValueError):
                        pass

            threads = [threading.Thread(target=drain, args=(process.stdout, True), daemon=True),
                       threading.Thread(target=drain, args=(process.stderr, False), daemon=True),
                       threading.Thread(target=write_input, daemon=True)]
            for thread in threads:
                thread.start()
                started_threads.append(thread)
            stop_at = time.monotonic() + min(self.spec.timeout_seconds, max(0, request.deadline - time.time()))
            while process.poll() is None:
                if exceeded.is_set():
                    reason = "output_limit_exceeded"
                    break
                if time.monotonic() >= stop_at:
                    reason = "timeout_outcome_unknown"
                    break
                time.sleep(0.005)
        except Exception:
            reason = "process_setup_failed"
        finally:
            # Guard setup/start as well as supervision: any post-spawn failure
            # must still stop the child and its group before returning.
            self._stop(process)
            for thread in started_threads:
                thread.join(timeout=2)
            if not any(thread.is_alive() for thread in started_threads):
                for stream in (process.stdin, process.stdout, process.stderr):
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
        if reason == "process_setup_failed":
            return ExecutionResult("result_uncertain", {"error": reason})
        if any(thread.is_alive() for thread in started_threads):
            return ExecutionResult("result_uncertain", {"error": "process_cleanup_uncertain"})
        if reason or exceeded.is_set() or stream_failed.is_set():
            return ExecutionResult("result_uncertain", {"error": reason or "output_outcome_unknown"})
        if process.returncode != 0:
            # This reports process failure, not rollback of effects it may have made.
            return ExecutionResult("failed", {"error": "command_failed", "exit_code": process.returncode})
        try:
            def unique_object(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("Duplicate result key")
                    value[key] = item
                return value
            result = json.loads(bytes(stdout).decode("utf-8"), object_pairs_hook=unique_object)
            if not isinstance(result, dict) or set(result) != {"status", "output"} or result["status"] not in {"succeeded", "failed"}:
                raise ValueError("Invalid result envelope")
            bounded_json(result["output"], limit)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return ExecutionResult("result_uncertain", {"error": "invalid_command_result"})
        except Exception:
            return ExecutionResult("result_uncertain", {"error": "invalid_command_result"})
        return ExecutionResult(result["status"], result["output"])
