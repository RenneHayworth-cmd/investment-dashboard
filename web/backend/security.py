"""Redact upstream error text before it reaches HTTP responses or logging."""
import logging
import os
import re
import traceback


def redact(value: str) -> str:
    for name in ("TICKFLOW_API_KEY", "WEB_PASSWORD_HASH"):
        secret = os.environ.get(name, "")
        if secret:
            value = value.replace(secret, "[已隐藏]")
    value = re.sub(r'https?://[^\s\)\]\'\"]+', '[数据源地址]', value)
    return value


def install_log_redaction():
    previous = logging.getLogRecordFactory()
    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact(''.join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return record
    logging.setLogRecordFactory(factory)
