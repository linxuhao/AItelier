"""Request-local checkpoint failure coordinates; never log exception text or locals."""
from contextvars import ContextVar
from functools import wraps
import json
import logging
import threading
import uuid

from fastapi import HTTPException

_current = ContextVar("checkpoint_observation", default=None)
logger = logging.getLogger(__name__)


def checkpoint_phase(phase, **identity):
    observation = _current.get()
    if observation is not None:
        observation["phase"] = phase
        observation.update(identity)


def checkpoint_failure(exc, *, translated=False):
    """Diagnostics are best effort, including when a logging handler raises."""
    try:
        observation = _current.get()
        if observation is None:
            return
        chain, seen = [], set()
        current = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            frames = []
            tb = current.__traceback__
            while tb is not None:
                code = tb.tb_frame.f_code
                frames.append({"file": code.co_filename, "line": tb.tb_lineno,
                               "function": code.co_name})
                tb = tb.tb_next
            chain.append({"type": type(current).__module__ + "." + type(current).__qualname__,
                          "frames": frames})
            current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        record = {**observation, "thread_id": threading.get_ident(),
                  "translated": translated, "exceptions": chain}
        # The default text formatter must retain the coordinates too.
        logger.error("checkpoint_failure %s", json.dumps(record),
                     extra={"checkpoint_observation": record})
    except BaseException:
        pass  # Observation must never replace the product exception.


def observe_checkpoint(function):
    @wraps(function)
    def observed(*args, **kwargs):
        if _current.get() is not None:
            return function(*args, **kwargs)
        token = _current.set({"request_id": uuid.uuid4().hex,
                              "route": function.__name__, "phase": "entry"})
        try:
            return function(*args, **kwargs)
        except BaseException as exc:
            if not isinstance(exc, HTTPException):
                checkpoint_failure(exc)
            raise
        finally:
            _current.reset(token)
    return observed
