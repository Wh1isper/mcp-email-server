import os
from collections import deque
from traceback import walk_tb

USER_DEFINED_LOG_LEVEL = os.getenv("MCP_EMAIL_SERVER_LOG_LEVEL", "INFO")

os.environ["LOGURU_LEVEL"] = USER_DEFINED_LOG_LEVEL

from loguru import logger  # noqa: E402

__all__ = ["log_safe_failure", "logger"]


def log_safe_failure(message: str, exc: BaseException) -> None:
    """Log a fixed message, exception type, and at most 20 value-free frames.

    Never attach the exception to Loguru: diagnostic sinks can expose credentials
    in frame locals, and exception messages/chains can contain provider data.
    Function names and line numbers retain useful failure locations without
    source lines, filesystem paths, locals, or exception text.
    """
    frames = deque(
        (f"{frame.f_code.co_name}:{line}" for frame, line in walk_tb(exc.__traceback__)),
        maxlen=20,
    )
    logger.opt(exception=False).warning(
        "{} ({}); Traceback: {}", message, type(exc).__name__, " -> ".join(frames) or "unavailable"
    )
