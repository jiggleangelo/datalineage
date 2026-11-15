from collections import OrderedDict
import logging
import re
import threading
import traceback

_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.[A-Za-z]{2,63}(?![\w.-])")


def _redact(value):
    return _EMAIL.sub("[redacted]", str(value))


class RedactingFilter(logging.Filter):
    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = _redact(message)
        record.args = ()
        if record.exc_info:
            rendered = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = _redact(rendered)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = _redact(record.exc_text)
        if record.stack_info:
            record.stack_info = _redact(record.stack_info)
        return True


def get_logger():
    logger = logging.getLogger("datalineage")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(RedactingFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class Metrics:
    def __init__(self, max_sessions=10000):
        self.values = {}
        self.sessions = OrderedDict()
        self.max_sessions = max(100, int(max_sessions))
        self._lock = threading.Lock()

    def inc(self, key):
        with self._lock:
            self.values[key] = self.values.get(key, 0) + 1

    def checker(self, sid):
        with self._lock:
            if sid in self.sessions:
                self.sessions.move_to_end(sid)
                return
            self.sessions[sid] = None
            while len(self.sessions) > self.max_sessions:
                self.sessions.popitem(last=False)
            self.values["checker_uses"] = self.values.get("checker_uses", 0) + 1

    def snapshot(self):
        with self._lock:
            return dict(self.values)
