"""Local process trust boundary and non-disclosing interface errors.

CLI and stdio inherit the local OS user's access to the database. This is not
remote authentication. Never build a Context from request bodies, model output,
HTTP headers, or MCP initialization metadata.
"""
from .config import Config
from .model import Context, HubError

_ERROR_MESSAGES = {
    "invalid_request": "Invalid request",
    "limit_exceeded": "Request exceeds a configured limit",
    "forbidden": "Required grant is not present",
    "not_found": "Task not found",
    "conflict": "Request conflicts with existing work",
    "expired": "Request has expired or has an invalid expiry",
    "loop_detected": "Task routing loop rejected",
    "rate_limited": "Request rate or pending task limit exceeded",
    "unsupported": "Operation or capability is not supported",
    "unavailable": "A required local dependency or resource is unavailable",
    "internal_error": "Operation failed; inspect the local installation",
}


def local_context(config: Config) -> Context:
    """Bind trusted operator configuration, never untrusted request fields."""
    return config.local_context()


def public_error(error: Exception) -> dict[str, dict[str, str]]:
    """Do not leak exception text, filenames, credentials, or request payloads."""
    code = error.code if isinstance(error, HubError) else "internal_error"
    if code not in _ERROR_MESSAGES:
        code = "internal_error"
    return {"error": {"code": code, "message": _ERROR_MESSAGES[code]}}
