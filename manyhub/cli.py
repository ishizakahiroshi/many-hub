"""Local CLI. It can run without any optional transport SDK installed."""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

from .auth import local_context, public_error
from .config import Config
from .executors.mock import MockExecutor
from .model import HubError
from .service import MAX_INPUT_BYTES, Service, bounded_json
from .store import SQLiteStore


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse normally echoes invalid values, which may contain secrets.
        self.exit(2, '{"error":{"code":"invalid_request","message":"Invalid command arguments; use --help"}}\n')


def _options(parser: argparse.ArgumentParser, *, root: bool = False) -> None:
    default = None if root else argparse.SUPPRESS
    parser.add_argument("--database", type=Path, default=default,
                        help="Local SQLite database (default: data/manyhub.sqlite3)")
    parser.add_argument("--command-config", type=Path, default=default,
                        help="Trusted local fixed-command Executor config (never task input)")
    parser.add_argument("--json", action="store_true", default=False if root else argparse.SUPPRESS,
                        help="Emit machine-readable JSON (errors go to stderr)")


def parser() -> argparse.ArgumentParser:
    result = _ArgumentParser(prog="manyhub", description="MANY Hub local task interfaces")
    _options(result, root=True)
    commands = result.add_subparsers(dest="command", required=True)
    _options(commands.add_parser("capabilities", help="Discover supported operations"))
    executor = commands.add_parser("executor", help="Inspect granted executors")
    _options(executor)
    executors = executor.add_subparsers(dest="action", required=True)
    _options(executors.add_parser("list"))
    task = commands.add_parser("task", help="Create and inspect owned work")
    _options(task)
    tasks = task.add_subparsers(dest="action", required=True)
    for action in ("create", "get", "list", "reply", "cancel"):
        operation = tasks.add_parser(action)
        _options(operation)
        if action in {"get", "reply", "cancel"}:
            operation.add_argument("task_id")
        if action in {"create", "reply"}:
            operation.add_argument("--input-file", required=True, type=Path,
                                   help="UTF-8 JSON request file; never identity or credentials")
        if action == "list":
            operation.add_argument("--limit", type=int, default=50)
    worker = commands.add_parser("worker", help="Process queued local work")
    _options(worker)
    worker.add_argument("--once", action="store_true", help="Process at most one task and exit")
    worker.add_argument("--poll-interval", type=_poll_interval, default=0.25)
    serve = commands.add_parser("serve", help="Run a local worker; opens no network listener")
    _options(serve)
    serve.add_argument("--local", action="store_true", required=True)
    serve.add_argument("--poll-interval", type=_poll_interval, default=0.25)
    mcp = commands.add_parser("mcp", help="Run the optional official MCP stdio server")
    _options(mcp)
    mcp.add_argument("--stdio", action="store_true", help="Use local stdio (the only supported transport)")
    return result


def _poll_interval(value: str) -> float:
    try:
        interval = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Poll interval must be between 0.01 and 60 seconds") from None
    if not math.isfinite(interval) or not 0.01 <= interval <= 60:
        raise argparse.ArgumentTypeError("Poll interval must be between 0.01 and 60 seconds")
    return interval


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def read_request(path: Path) -> dict[str, Any]:
    """Bound the raw read before parsing, then apply the canonical JSON bound."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
    except OSError:
        raise HubError("invalid_request", "Cannot read request file") from None
    if len(raw) > MAX_INPUT_BYTES:
        raise HubError("limit_exceeded", "Request file is too large")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise HubError("invalid_request", "Invalid JSON request file") from None
    bounded_json(value)
    return value


def _emit(value: Any, *, compact: bool, error: bool = False) -> None:
    print(json.dumps(value, ensure_ascii=True, allow_nan=False,
                     indent=None if compact else 2, separators=(",", ":") if compact else None),
          file=sys.stderr if error else sys.stdout)


def _dispatch(args: argparse.Namespace, service: Service, config: Config, context=None) -> Any:
    context = context or local_context(config)
    if args.command == "capabilities":
        return service.capabilities(context)
    if args.command == "executor":
        return service.executor_list(context)
    if args.command == "task":
        if args.action == "create":
            return service.task_create(context, read_request(args.input_file))
        if args.action == "get":
            return service.task_get(context, args.task_id)
        if args.action == "list":
            return service.task_list(context, args.limit)
        if args.action == "reply":
            return service.task_reply(context, args.task_id, read_request(args.input_file))
        return service.task_cancel(context, args.task_id)
    if args.command == "mcp":
        # Import only at the boundary: CLI/Core need no MCP dependency.
        try:
            from .adapters.mcp import create_server
        except ImportError:
            raise HubError("unavailable", "Install the optional MCP extra") from None
        create_server(service, context).run(transport="stdio")
        return None
    if args.command == "worker" and args.once:
        return {"processed": service.run_once()}
    while True:
        if not service.run_once():
            time.sleep(args.poll_interval)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store = None
    try:
        config = Config(database=args.database or Config().database)
        store = SQLiteStore(config.database)
        executors = [MockExecutor()]
        context = local_context(config)
        if args.command_config is not None:
            # This is explicit operator configuration, never a task/MCP argument.
            from .executors.command import CommandExecutor, CommandSpec
            definition = read_request(args.command_config)
            allowed = {"executor_id", "executable", "arguments", "workspace", "allowed_executables", "timeout_seconds", "max_output_bytes"}
            required = {"executor_id", "executable", "arguments", "workspace", "allowed_executables"}
            if set(definition) - allowed or not required <= set(definition):
                raise HubError("invalid_request", "Invalid command configuration")
            if (not isinstance(definition["executable"], str) or not isinstance(definition["workspace"], str)
                    or not isinstance(definition["arguments"], list) or not isinstance(definition["allowed_executables"], list)
                    or any(not isinstance(value, str) for value in definition["allowed_executables"])):
                raise HubError("invalid_request", "Invalid command configuration")
            executor = CommandExecutor(CommandSpec(
                executor_id=definition["executor_id"], executable=Path(definition["executable"]),
                arguments=tuple(definition["arguments"]), workspace=Path(definition["workspace"]),
                allowed_executables=tuple(Path(value) for value in definition["allowed_executables"]),
                timeout_seconds=definition.get("timeout_seconds", 10),
                max_output_bytes=definition.get("max_output_bytes", 65536)))
            executors.append(executor)
            context = dataclasses.replace(context, scopes=context.scopes | {f"executor:use:{executor.executor_id}"})
        service = Service(store, executors, max_pending=config.max_pending_tasks,
                          max_requests_per_minute=config.max_requests_per_minute)
        result = _dispatch(args, service, config, context)
        if result is not None:
            _emit(result, compact=args.json)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # Never expose raw SQLite/SDK/OS errors or potentially sensitive input.
        _emit(public_error(error), compact=args.json, error=True)
        return 1
    finally:
        if store is not None:
            store.close()
