"""Process-wide safety net: no outbound HTTP call may wait forever.

Some third-party fetchers (e.g. AkShare's EastMoney board history) call
``requests.get`` without a timeout. From the server, EastMoney sometimes stops
answering on an open connection; with no read timeout that call blocks for
good, and because the coordinator refreshes serially, every later ETF, quote
and formal-close update stalls behind it (seen 2026-09-28 23:33 to 09-29).
Calls that pass their own timeout are left untouched.
"""
import requests

# (connect, read) seconds; read is the maximum gap between received bytes.
DEFAULT_TIMEOUT = (10, 30)

_installed = False


def install_default_request_timeout(timeout=DEFAULT_TIMEOUT) -> None:
    global _installed
    if _installed:
        return
    original = requests.Session.request

    def request(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = timeout
        return original(self, method, url, **kwargs)

    request.__wrapped__ = original
    requests.Session.request = request
    _installed = True
