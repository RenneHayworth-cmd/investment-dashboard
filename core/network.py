"""HTTP timeout guard shared by web, Streamlit and standalone schedulers."""
import threading

import requests

DEFAULT_TIMEOUT = (10, 30)
_lock = threading.Lock()


def install_default_request_timeout(timeout=DEFAULT_TIMEOUT) -> None:
    with _lock:
        if getattr(requests.Session.request, "_market_timeout_installed", False):
            return
        original = requests.Session.request

        def request(self, method, url, **kwargs):
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = timeout
            return original(self, method, url, **kwargs)

        request.__wrapped__ = original
        request._market_timeout_installed = True
        requests.Session.request = request
