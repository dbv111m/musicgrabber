"""
Regression checks for the shared test client's connection handling.

MusicGrabber's uvicorn closes idle keep-alive connections after five seconds.
urllib3 notices a socket that is already dead and quietly opens a fresh one, so
the ordinary case never troubled anybody; the case that did was the close
crossing in flight with our send, surfacing as ConnectionError(RemoteDisconnected)
and taking out roughly one slow run in two, a different test each time. The
client now retries that. These tests hold it to that, and to not retrying things
it has no business retrying.

Entirely offline: the server below is a socket that misbehaves on purpose.
"""

import socket
import threading

import pytest
import requests

from conftest import QueueHistorySafeSession


class RudeServer:
    """A server that hangs up on its first `drops` callers, then answers.

    Hanging up *after* reading the request is what produces the client-side
    RemoteDisconnected we are actually chasing; closing before reading gives a
    connection reset instead, which is a different animal.
    """

    def __init__(self, drops=1, status=200):
        self.drops = drops
        self.status = status
        self.connections = 0
        self.requests_read = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/api/download"

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            try:
                self._read_request(conn)
                self.requests_read += 1
                if self.connections <= self.drops:
                    conn.close()          # hang up mid-conversation, saying nothing
                    continue
                body = b"ok"
                conn.sendall(
                    f"HTTP/1.1 {self.status} X\r\nContent-Length: {len(body)}\r\n"
                    f"Connection: close\r\n\r\n".encode() + body
                )
            except OSError:
                pass
            finally:
                conn.close()

    @staticmethod
    def _read_request(conn):
        """Drain headers and any body, so the client has finished sending."""
        conn.settimeout(2)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        while len(rest) < length:
            chunk = conn.recv(4096)
            if not chunk:
                break
            rest += chunk

    def close(self):
        self._sock.close()


@pytest.fixture
def rude_server():
    servers = []

    def _make(**kwargs):
        server = RudeServer(**kwargs)
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.close()


def test_a_bare_session_really_does_die_on_this(rude_server):
    """Establishes that the fixture below is testing something real, rather than
    congratulating itself on a server that was never rude in the first place."""
    server = rude_server(drops=1)

    with pytest.raises(requests.exceptions.ConnectionError):
        requests.Session().post(server.url, json={}, timeout=5)


def test_test_client_retries_a_safe_get_after_the_server_hangs_up(rude_server):
    server = rude_server(drops=1)
    session = QueueHistorySafeSession(allow_history_deletion=False)

    response = session.get(server.url, timeout=5)

    assert response.status_code == 200
    assert server.connections == 2, "should have retried onto a second connection"


def test_a_fully_received_post_is_not_replayed(rude_server):
    """The response is ambiguous after the server reads the whole body. It may
    have committed the mutation, so replaying it could perform the work twice."""
    server = rude_server(drops=1)
    session = QueueHistorySafeSession(allow_history_deletion=False)

    with pytest.raises(requests.exceptions.ConnectionError):
        session.post(server.url, json={}, timeout=5)

    assert server.connections == 1
    assert server.requests_read == 1


def test_a_500_is_not_retried_away(rude_server):
    """Retrying a server error until it stops being one is how you ship a bug."""
    server = rude_server(drops=0, status=500)
    session = QueueHistorySafeSession(allow_history_deletion=False)

    response = session.get(server.url, timeout=5)

    assert response.status_code == 500
    assert server.connections == 1, "a 500 is a finding, not something to paper over"


def test_retrying_did_not_cost_us_the_history_guard(rude_server):
    """The retry adapter is mounted in the same constructor as the safety net;
    a careless edit to one could plausibly take out the other."""
    session = QueueHistorySafeSession(allow_history_deletion=False)

    with pytest.raises(RuntimeError, match="Queue history"):
        session.delete("http://localhost:38274/api/jobs/cleanup")
