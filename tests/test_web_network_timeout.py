import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _silent_server():
    """Accepts connections and never answers: the stall seen from EastMoney."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    held = []

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            held.append(conn)

    threading.Thread(target=serve, daemon=True).start()
    return server, held


def _run(code: str, timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=ROOT,
                          capture_output=True, text=True, timeout=timeout)


def test_untimed_request_is_bounded_after_install():
    server, _ = _silent_server()
    port = server.getsockname()[1]
    try:
        started = time.monotonic()
        result = _run(f"""
            import requests
            from web.backend.network import install_default_request_timeout
            install_default_request_timeout((2, 1))
            try:
                requests.get("http://127.0.0.1:{port}/")  # like AkShare: no timeout argument
            except requests.exceptions.ReadTimeout:
                print("timed-out")
        """)
        assert result.stdout.strip() == "timed-out", result.stderr
        assert time.monotonic() - started < 15
    finally:
        server.close()


def test_explicit_timeouts_are_left_alone_and_install_is_idempotent():
    result = _run("""
        import requests
        from web.backend import network
        seen = []
        original = requests.Session.request
        def spy(self, method, url, **kwargs):
            seen.append(kwargs.get("timeout"))
            raise RuntimeError("stop")
        requests.Session.request = spy
        network.install_default_request_timeout((10, 30))
        network.install_default_request_timeout((1, 1))  # second call must not re-wrap
        for kwargs in ({}, {"timeout": 5}, {"timeout": None}):
            try:
                requests.get("http://example.invalid/", **kwargs)
            except RuntimeError:
                pass
        print(seen)
    """)
    assert result.stdout.strip() == "[(10, 30), 5, (10, 30)]", result.stderr


def test_backend_app_installs_the_guard():
    result = _run("""
        import requests
        import web.backend.app  # noqa: F401
        print(hasattr(requests.Session.request, "__wrapped__"))
    """, timeout=60)
    assert result.stdout.strip().splitlines()[-1] == "True", result.stderr
