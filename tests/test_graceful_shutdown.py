"""A container/k8s stop is a SIGTERM, and it must shut the server down cleanly:
stop accepting, drain in-flight requests (non-daemon worker threads), release
the app. These pin the drain-window setting and the shutdown coordination
without spawning a real process or raising real signals."""

import threading

from hallpass.cli import _serve_until_signal
from hallpass.http_server import serve


def test_serve_uses_non_daemon_threads_for_a_drain_window():
    from hallpass import dev_app

    app, _ = dev_app()
    server = serve(app, host="127.0.0.1", port=0)  # port 0 -> OS picks a free port
    try:
        # daemon_threads=False means shutdown joins in-flight requests instead
        # of the process exiting out from under them.
        assert server.daemon_threads is False
    finally:
        server.server_close()
        app.close()


class _FakeServer:
    def __init__(self) -> None:
        self.shut = False
        self.closed = False
        self._running = threading.Event()

    def serve_forever(self) -> None:
        self._running.wait()  # block until shutdown() releases it

    def shutdown(self) -> None:
        self.shut = True
        self._running.set()

    def server_close(self) -> None:
        self.closed = True


class _FakeApp:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_stop_event_triggers_clean_shutdown():
    """Setting the stop event (what a SIGTERM/SIGINT handler does) must stop the
    server AND release the app -- the path the old SIGINT-only loop skipped on a
    container stop."""
    server = _FakeServer()
    app = _FakeApp()
    stop = threading.Event()
    runner = threading.Thread(
        target=_serve_until_signal,
        kwargs={"server": server, "app": app, "install_signals": False, "stop": stop},
    )
    runner.start()
    stop.set()  # stand in for the signal handler
    runner.join(timeout=5)
    assert not runner.is_alive()
    assert server.shut is True  # stopped accepting
    assert server.closed is True  # socket released
    assert app.closed is True  # app resources released (vault, etc.)
