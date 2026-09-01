"""
Structured logging for the agent.

Every log line is a JSON object carrying the job id, so a single review can be
followed end to end across the webhook, the graph nodes and the sandbox. Node
timings and failures are emitted automatically by `instrument_node`, which is
what makes "where did this run go wrong" answerable after the fact.
"""
import contextvars
import functools
import inspect
import json
import logging
import sys
import time
import traceback
from contextlib import contextmanager
from typing import Any, Callable, Optional

job_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("job_id", default=None)

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        job_id = job_id_var.get()
        if job_id:
            payload["job_id"] = job_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["error"] = "".join(traceback.format_exception(*record.exc_info)).strip()
        return json.dumps(payload, default=str)


class PlainFormatter(logging.Formatter):
    """Human-readable output for local CLI runs."""

    def format(self, record: logging.LogRecord) -> str:
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        job_id = job_id_var.get()
        prefix = f"[{job_id[:8]}] " if job_id else ""
        suffix = " " + " ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""
        return f"{prefix}{record.levelname:<7} {record.getMessage()}{suffix}"


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else PlainFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # These libraries are chatty at INFO and drown out agent events.
    for noisy in ("httpx", "httpcore", "urllib3", "openai"):
        logging.getLogger(noisy).setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


@contextmanager
def job_context(job_id: str):
    """Binds a job id to every log line emitted inside the block."""
    token = job_id_var.set(job_id)
    try:
        yield
    finally:
        job_id_var.reset(token)


def instrument_node(name: str, fn: Callable) -> Callable:
    """
    Wraps a graph node so each execution logs start, duration and outcome.
    Exceptions are logged with a traceback and re-raised.
    """
    logger = get_logger(f"agent.node.{name}")

    def _summarize(result: Any) -> dict:
        if not isinstance(result, dict):
            return {}
        summary = {}
        for key, value in result.items():
            if isinstance(value, (list, dict)):
                summary[f"{key}_count"] = len(value)
            elif isinstance(value, (str, int, float, bool)):
                summary[key] = value
        return summary

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(state, *args, **kwargs):
            started = time.perf_counter()
            logger.info("node.start", extra={"node": name})
            try:
                result = await fn(state, *args, **kwargs)
            except Exception:
                logger.exception("node.error", extra={"node": name,
                                                      "duration_ms": round((time.perf_counter() - started) * 1000)})
                raise
            logger.info("node.done", extra={"node": name,
                                            "duration_ms": round((time.perf_counter() - started) * 1000),
                                            **_summarize(result)})
            return result
        return async_wrapper

    @functools.wraps(fn)
    def wrapper(state, *args, **kwargs):
        started = time.perf_counter()
        logger.info("node.start", extra={"node": name})
        try:
            result = fn(state, *args, **kwargs)
        except Exception:
            logger.exception("node.error", extra={"node": name,
                                                  "duration_ms": round((time.perf_counter() - started) * 1000)})
            raise
        logger.info("node.done", extra={"node": name,
                                        "duration_ms": round((time.perf_counter() - started) * 1000),
                                        **_summarize(result)})
        return result

    return wrapper
