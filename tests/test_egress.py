import contextlib
import io
import re
import shutil
import signal
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import bench.egress as egress

REAL_KEY = "REALKEY-0123456789"
CLIENT_KEY = "CLIENTKEY-dummy"
TIMEOUT = 5
OK_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"


def socket_pair():
    a, b = socket.socketpair()
    a.settimeout(3)
    b.settimeout(3)
    return a, b


def tcp_pair():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(3)
    client = socket.create_connection(listener.getsockname(), timeout=3)
    server, _ = listener.accept()
    listener.close()
    server.settimeout(3)
    return client, server


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request_head(*lines, request_line="POST /v1/chat?x=1 HTTP/1.1"):
    return ("\r\n".join((request_line,) + lines) + "\r\n\r\n").encode("latin-1")


def header_lines(rewritten):
    text = rewritten.decode("latin-1")
    assert text.endswith("\r\n\r\n")
    return text[: -4].split("\r\n")


def header_names(lines):
    return [line.split(":", 1)[0].lower() for line in lines]


class ParseRouteTests(unittest.TestCase):
    def test_good_routes(self):
        self.assertEqual(egress.parse_route("8080=example.com:80"), (8080, "example.com", 80))
        self.assertEqual(egress.parse_route("1=a-b.c:65535"), (1, "a-b.c", 65535))
        self.assertEqual(egress.parse_route("9=10.0.0.1:11434"), (9, "10.0.0.1", 11434))

    def test_invalid_routes_raise_usage_error_naming_spec(self):
        bad = [
            "0=h:80", "70000=h:80", "abc=h:80", "-1=h:80", "8080=h:0", "8080=h:70000", "8080=h:abc",
            "8080=h:", "8080h:80", "8080=:80", "8080=h", "", "8080=[::1]:80", "8080=::1:80",
            "8080=ex ample:80", "8080=h:80:90", "8080=h_x:80",
        ]
        for spec in bad:
            with self.subTest(spec=spec):
                with self.assertRaises(egress.UsageError) as caught:
                    egress.parse_route(spec)
                self.assertIn(spec, str(caught.exception))


class ReadKeyTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_strips_trailing_whitespace(self):
        path = self.dir / "key"
        path.write_text(REAL_KEY + " \r\n\n")
        self.assertEqual(egress.read_key(path), REAL_KEY)
        self.assertEqual(egress.read_key(str(path)), REAL_KEY)

    def test_missing_empty_and_unreadable_fail_without_key_text(self):
        empty = self.dir / "empty"
        empty.write_text(" \n\t\n")
        for path in (self.dir / "missing", empty, self.dir):
            with self.subTest(path=path.name):
                with self.assertRaises(egress.UsageError) as caught:
                    egress.read_key(path)
                self.assertEqual(str(caught.exception), "key file missing or empty")


class ReadHeadTests(unittest.TestCase):
    def test_head_split_across_several_sends(self):
        reader, writer = socket_pair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        pieces = [b"GET / HT", b"TP/1.1\r\nHost: a\r", b"\n\r", b"\n"]

        def send():
            for piece in pieces:
                writer.sendall(piece)
                time.sleep(0.05)

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        head, rest = egress.read_head(reader, 4096)
        thread.join(3)
        self.assertEqual(head, b"".join(pieces))
        self.assertEqual(rest, b"")

    def test_body_arriving_with_head_is_returned_as_rest(self):
        reader, writer = socket_pair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        head = b"POST / HTTP/1.1\r\nContent-Length: 5\r\n\r\n"
        writer.sendall(head + b"hello")
        got_head, rest = egress.read_head(reader, 4096)
        self.assertEqual(got_head, head)
        self.assertEqual(rest, b"hello")

    def test_too_large_without_terminator(self):
        reader, writer = socket_pair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        writer.sendall(b"GET / HTTP/1.1\r\n" + b"X-Pad: " + b"a" * 300)
        with self.assertRaisesRegex(ValueError, "head too large"):
            egress.read_head(reader, 100)

    def test_peer_closing_early(self):
        for partial in (b"", b"GET / HT", b"GET / HTTP/1.1\r\nHost: a\r\n"):
            with self.subTest(partial=partial):
                reader, writer = socket_pair()
                self.addCleanup(reader.close)
                writer.sendall(partial)
                writer.close()
                with self.assertRaisesRegex(ValueError, "connection closed"):
                    egress.read_head(reader, 4096)


class RewriteHeadTests(unittest.TestCase):
    HEAD = request_head(
        "Host: client.local:1234",
        "Authorization: Bearer " + CLIENT_KEY,
        "X-API-Key: " + CLIENT_KEY,
        "Content-Type: application/json",
        "Connection: keep-alive",
        "Keep-Alive: timeout=5",
        "Proxy-Connection: keep-alive",
        "Proxy-Authorization: Basic abc",
        "TE: trailers",
        "Upgrade: websocket",
        "Content-Length: 12",
        "Transfer-Encoding: chunked",
        "X-Custom-Header: Value",
        "lowercase-name: v",
    )

    def rewrite(self, head=None):
        return egress.rewrite_head(head or self.HEAD, REAL_KEY, "up.example", 8080)

    def test_auth_headers_replaced_exactly_once(self):
        out = self.rewrite()
        self.assertIsInstance(out, bytes)
        self.assertNotIn(CLIENT_KEY.encode(), out)
        lines = header_lines(out)[1:]
        names = header_names(lines)
        self.assertEqual(names.count("authorization"), 1)
        self.assertEqual(names.count("x-api-key"), 1)
        self.assertIn("Authorization: Bearer " + REAL_KEY, lines)
        self.assertIn("x-api-key: " + REAL_KEY, lines)

    def test_auth_header_names_removed_case_insensitively(self):
        head = request_head("AUTHORIZATION: Bearer " + CLIENT_KEY, "x-API-key: " + CLIENT_KEY)
        out = self.rewrite(head)
        self.assertNotIn(CLIENT_KEY.encode(), out)
        self.assertEqual(header_names(header_lines(out)[1:]).count("authorization"), 1)

    def test_hop_headers_removed_and_connection_close_added(self):
        lines = header_lines(self.rewrite())[1:]
        names = header_names(lines)
        for hop in ("proxy-connection", "keep-alive", "proxy-authorization", "te", "upgrade"):
            self.assertNotIn(hop, names)
        self.assertEqual([line for line in lines if line.lower().startswith("connection:")], ["Connection: close"])

    def test_host_replaced_or_added(self):
        lines = header_lines(self.rewrite())[1:]
        self.assertEqual([line for line in lines if line.lower().startswith("host:")], ["Host: up.example:8080"])
        no_host = self.rewrite(request_head("Accept: */*"))
        host_lines = [line for line in header_lines(no_host)[1:] if line.lower().startswith("host:")]
        self.assertEqual(host_lines, ["Host: up.example:8080"])

    def test_other_headers_keep_order_case_and_body_framing(self):
        out = self.rewrite()
        lines = header_lines(out)
        self.assertEqual(lines[0], "POST /v1/chat?x=1 HTTP/1.1")
        injected = {"host", "authorization", "x-api-key", "connection"}
        others = [line for line in lines[1:] if line.split(":", 1)[0].lower() not in injected]
        self.assertEqual(
            others,
            [
                "Content-Type: application/json",
                "Content-Length: 12",
                "Transfer-Encoding: chunked",
                "X-Custom-Header: Value",
                "lowercase-name: v",
            ],
        )
        self.assertTrue(out.endswith(b"\r\n\r\n"))
        self.assertNotIn(b"\r\n\r\n", out[:-4])

    def test_http_1_0_and_other_methods_accepted(self):
        out = self.rewrite(request_head("Accept: */*", request_line="GET /v1/models HTTP/1.0"))
        self.assertEqual(header_lines(out)[0], "GET /v1/models HTTP/1.0")

    def test_latin1_header_values_round_trip(self):
        head = "GET / HTTP/1.1\r\nX-Name: café\r\n\r\n".encode("latin-1")
        self.assertIn("X-Name: café".encode("latin-1"), self.rewrite(head))

    def test_malformed_heads_raise_value_error(self):
        cases = {
            "absolute-form": b"GET http://evil.example/ HTTP/1.1\r\nHost: a\r\n\r\n",
            "authority-form": b"CONNECT evil.example:443 HTTP/1.1\r\nHost: a\r\n\r\n",
            "missing version": b"GET /\r\nHost: a\r\n\r\n",
            "bad version": b"GET / SPDY/3\r\nHost: a\r\n\r\n",
            "no colon": b"GET / HTTP/1.1\r\nHost a\r\n\r\n",
            "obs-fold space": b"GET / HTTP/1.1\r\nX-A: b\r\n continued\r\n\r\n",
            "obs-fold tab": b"GET / HTTP/1.1\r\nX-A: b\r\n\tcontinued\r\n\r\n",
            "trace method": b"TRACE / HTTP/1.1\r\nHost: a\r\n\r\n",
            "unknown method": b"BREW / HTTP/1.1\r\nHost: a\r\n\r\n",
        }
        for name, head in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    self.rewrite(head)


class RelayTests(unittest.TestCase):
    def start_relay(self, a, b, idle_timeout=5):
        errors = []

        def run():
            try:
                egress.relay(a, b, idle_timeout)
            except BaseException as error:  # noqa: BLE001 - report any escape
                errors.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, errors

    def test_bidirectional_copy_half_close_and_cleanup(self):
        client_out, client_in = socket_pair()
        upstream_in, upstream_out = socket_pair()
        for sock in (client_out, upstream_out):
            self.addCleanup(sock.close)
        thread, errors = self.start_relay(client_in, upstream_in)

        client_out.sendall(b"ping")
        self.assertEqual(upstream_out.recv(100), b"ping")
        upstream_out.sendall(b"pong")
        self.assertEqual(client_out.recv(100), b"pong")

        client_out.shutdown(socket.SHUT_WR)
        self.assertEqual(upstream_out.recv(100), b"")  # half-close propagated
        upstream_out.sendall(b"tail")  # other direction still open
        self.assertEqual(client_out.recv(100), b"tail")

        upstream_out.shutdown(socket.SHUT_WR)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(client_in.fileno(), -1)
        self.assertEqual(upstream_in.fileno(), -1)
        self.assertEqual(client_out.recv(100), b"")

    def test_large_payload_is_copied_intact(self):
        client_out, client_in = socket_pair()
        upstream_in, upstream_out = socket_pair()
        for sock in (client_out, upstream_out):
            self.addCleanup(sock.close)
        thread, errors = self.start_relay(client_in, upstream_in)
        payload = bytes(range(256)) * 1024
        threading.Thread(
            target=lambda: (client_out.sendall(payload), client_out.shutdown(socket.SHUT_WR)), daemon=True
        ).start()
        received = bytearray()
        while True:
            data = upstream_out.recv(65536)
            if not data:
                break
            received += data
        upstream_out.shutdown(socket.SHUT_WR)
        thread.join(3)
        self.assertEqual(bytes(received), payload)
        self.assertEqual(errors, [])

    def test_idle_timeout_returns_and_closes_both(self):
        client_out, client_in = socket_pair()
        upstream_in, upstream_out = socket_pair()
        for sock in (client_out, upstream_out):
            self.addCleanup(sock.close)
        started = time.monotonic()
        thread, errors = self.start_relay(client_in, upstream_in, idle_timeout=0.3)
        thread.join(3)
        elapsed = time.monotonic() - started
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 1.3)
        self.assertEqual(errors, [])
        self.assertEqual(client_in.fileno(), -1)
        self.assertEqual(upstream_in.fileno(), -1)

    def test_traffic_resets_idle_timer(self):
        client_out, client_in = socket_pair()
        upstream_in, upstream_out = socket_pair()
        for sock in (client_out, upstream_out):
            self.addCleanup(sock.close)
        started = time.monotonic()
        thread, errors = self.start_relay(client_in, upstream_in, idle_timeout=0.3)
        for _ in range(2):
            time.sleep(0.2)
            client_out.sendall(b"x")
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(time.monotonic() - started, 0.6)
        self.assertEqual(errors, [])

    def test_peer_reset_does_not_raise(self):
        client_out, client_in = tcp_pair()
        upstream_in, upstream_out = socket_pair()
        self.addCleanup(upstream_out.close)
        self.addCleanup(client_out.close)
        thread, errors = self.start_relay(client_in, upstream_in)
        client_out.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        client_out.close()  # sends RST
        time.sleep(0.1)
        try:
            upstream_out.sendall(b"into the void")
        except OSError:
            pass
        upstream_out.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(client_in.fileno(), -1)
        self.assertEqual(upstream_in.fileno(), -1)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _CaptureHandler(socketserver.BaseRequestHandler):
    """Records the raw request (head plus Content-Length body), then replies with canned chunks."""

    def handle(self):
        sock = self.request
        sock.settimeout(TIMEOUT)
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            data = sock.recv(4096)
            if not data:
                break
            buffer += data
        head, _, body = buffer.partition(b"\r\n\r\n")
        match = re.search(rb"(?im)^content-length:\s*(\d+)", head)
        while match and len(body) < int(match.group(1)):
            data = sock.recv(4096)
            if not data:
                break
            body += data
        self.server.requests.append(head + b"\r\n\r\n" + body)
        for index, chunk in enumerate(self.server.chunks):
            if index:
                time.sleep(self.server.delay)
            sock.sendall(chunk)


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(TIMEOUT)
        while True:
            data = self.request.recv(65536)
            if not data:
                return
            self.request.sendall(data)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.stderr = io.StringIO()
        patcher = mock.patch.object(sys, "stderr", self.stderr)
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, server):
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def upstream(self, handler=_CaptureHandler, chunks=(OK_RESPONSE,), delay=0.0):
        server = self.serve(_Server(("127.0.0.1", 0), handler))
        server.requests, server.chunks, server.delay = [], list(chunks), delay
        return server, server.server_address[1]

    def gateway(self, http=(), tcp=(), max_head=65536):
        servers = egress.start("127.0.0.1", list(http), list(tcp), REAL_KEY, 5, max_head)
        for server in servers:
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        return [server.server_address[1] for server in servers]

    def exchange(self, port, *parts, delay=0.0):
        with socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT) as sock:
            for index, part in enumerate(parts):
                if index:
                    time.sleep(delay)
                sock.sendall(part)
            received = bytearray()
            while True:
                data = sock.recv(65536)
                if not data:
                    return bytes(received)
                received += data

    def wait_for_log(self, text):
        deadline = time.monotonic() + 3
        while text not in self.stderr.getvalue() and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.stderr.getvalue()

    def get(self, target="/v1/models"):
        return request_head("Host: agent.local", "Authorization: Bearer " + CLIENT_KEY, request_line="GET %s HTTP/1.1" % target)

    def test_http_injects_key_and_relays_response(self):
        up, up_port = self.upstream()
        (port,) = self.gateway(http=[(0, "127.0.0.1", up_port)])
        response = self.exchange(port, self.get())
        self.assertEqual(response, OK_RESPONSE)
        (seen,) = up.requests
        self.assertIn(("Authorization: Bearer " + REAL_KEY).encode(), seen)
        self.assertIn(("x-api-key: " + REAL_KEY).encode(), seen)
        self.assertNotIn(CLIENT_KEY.encode(), seen)
        self.assertIn(("Host: 127.0.0.1:%d" % up_port).encode(), seen)
        self.assertTrue(seen.startswith(b"GET /v1/models HTTP/1.1\r\n"))
        self.assertIn(b"Connection: close", seen)

    def test_streamed_response_arrives_complete(self):
        chunks = [b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"]
        chunks += [("data: %d\n\n" % i).encode() for i in range(5)]
        _, up_port = self.upstream(chunks=chunks, delay=0.05)
        (port,) = self.gateway(http=[(0, "127.0.0.1", up_port)])
        self.assertEqual(self.exchange(port, self.get()), b"".join(chunks))

    def test_post_body_sent_after_head_is_relayed(self):
        up, up_port = self.upstream()
        (port,) = self.gateway(http=[(0, "127.0.0.1", up_port)])
        head = request_head("Host: a", "Authorization: Bearer " + CLIENT_KEY, "Content-Length: 11")
        self.assertEqual(self.exchange(port, head, b"hello world", delay=0.15), OK_RESPONSE)
        self.assertTrue(up.requests[0].endswith(b"\r\n\r\nhello world"))
        self.assertNotIn(CLIENT_KEY.encode(), up.requests[0])

    def test_post_body_sent_with_head_is_relayed(self):
        up, up_port = self.upstream()
        (port,) = self.gateway(http=[(0, "127.0.0.1", up_port)])
        head = request_head("Host: a", "Content-Length: 11")
        self.assertEqual(self.exchange(port, head + b"hello world"), OK_RESPONSE)
        self.assertTrue(up.requests[0].endswith(b"\r\n\r\nhello world"))

    def test_malformed_requests_get_400_and_never_reach_upstream(self):
        up, up_port = self.upstream()
        (port,) = self.gateway(http=[(0, "127.0.0.1", up_port)], max_head=256)
        bad = [
            b"BOGUS\r\n\r\n",
            b"GET http://evil.example/ HTTP/1.1\r\nHost: a\r\n\r\n",
            b"GET / HTTP/1.1\r\nno-colon-here\r\n\r\n",
            b"GET / HTTP/1.1\r\nX-Pad: " + b"a" * 600 + b"\r\n\r\n",
        ]
        for request in bad:
            with self.subTest(request=request[:30]):
                response = self.exchange(port, request)
                self.assertTrue(response.startswith(b"HTTP/1.1 400 Bad Request\r\n"), response)
        self.assertEqual(up.requests, [])

    def test_refused_upstream_gets_502(self):
        (port,) = self.gateway(http=[(0, "127.0.0.1", free_port())])
        response = self.exchange(port, self.get())
        self.assertTrue(response.startswith(b"HTTP/1.1 502 Bad Gateway\r\n"), response)

    def test_tcp_route_echoes_raw_bytes(self):
        _, up_port = self.upstream(handler=_EchoHandler)
        (port,) = self.gateway(tcp=[(0, "127.0.0.1", up_port)])
        payload = b"\x00\xff not http \r\n\r\n" * 1000
        with socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT) as sock:
            threading.Thread(
                target=lambda: (sock.sendall(payload), sock.shutdown(socket.SHUT_WR)), daemon=True
            ).start()
            received = bytearray()
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                received += data
        self.assertEqual(bytes(received), payload)

    def test_log_path_excludes_http_version(self):
        up, up_port = self.upstream()
        (port,) = self.gateway(http=[(0, "127.0.0.1", up_port)])
        self.exchange(port, self.get("/v1/models"))
        log = self.wait_for_log("->")
        self.assertIn("http %d GET /v1/models -> 127.0.0.1:%d\n" % (port, up_port), log)
        self.assertNotIn("HTTP/1.1", log)

    def test_connections_beyond_the_limit_are_closed(self):
        up, up_port = self.upstream(handler=_EchoHandler)
        servers = egress.start("127.0.0.1", [], [(0, "127.0.0.1", up_port)], REAL_KEY, 5, 65536, max_connections=1)
        for server in servers:
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        port = servers[0].server_address[1]
        with socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT) as first:
            first.sendall(b"one")
            self.assertEqual(first.recv(16), b"one")
            with socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT) as second:
                second.sendall(b"two")
                self.assertEqual(second.recv(16), b"")
        deadline = time.monotonic() + 3
        while True:
            with socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT) as third:
                third.sendall(b"three")
                if third.recv(16) == b"three":
                    break
            self.assertLess(time.monotonic(), deadline, "slot was not released")
            time.sleep(0.05)

    def test_default_connection_limit_is_bounded(self):
        self.assertIsInstance(egress.DEFAULT_MAX_CONNECTIONS, int)
        self.assertTrue(16 <= egress.DEFAULT_MAX_CONNECTIONS <= 1024)

    def test_logs_contain_route_but_never_key_or_query(self):
        up, up_port = self.upstream()
        (port,) = self.gateway(http=[(0, "127.0.0.1", up_port)])
        self.exchange(port, self.get("/v1/models?token=hunter2&q=secretquery"))
        log = self.wait_for_log("->")
        self.assertIn("http %d GET /v1/models -> 127.0.0.1:%d" % (port, up_port), log)
        for secret in (REAL_KEY, CLIENT_KEY, "hunter2", "secretquery", "token="):
            self.assertNotIn(secret, log)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        # A broken main that starts serving would block forever; the alarm turns that into a failure.
        previous = signal.signal(signal.SIGALRM, self.on_alarm)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        signal.alarm(8)
        self.addCleanup(signal.alarm, 0)

    @staticmethod
    def on_alarm(signum, frame):
        raise AssertionError("main() did not return; it must exit before serving")

    def key_file(self, text=REAL_KEY + "\n"):
        path = self.dir / "key"
        path.write_text(text)
        return str(path)

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = egress.main(argv)
            except SystemExit as exit_error:
                code = exit_error.code
        return code, out.getvalue(), err.getvalue()

    def assert_usage_failure(self, argv, secret=REAL_KEY):
        code, out, err = self.run_main(argv)
        self.assertEqual(code, 2)
        self.assertNotIn("ready", out)
        self.assertTrue(err.strip())
        self.assertNotIn(secret, out + err)

    def test_no_routes(self):
        self.assert_usage_failure([])
        self.assert_usage_failure(["--bind", "127.0.0.1"])

    def test_duplicate_listen_port(self):
        self.assert_usage_failure(["--tcp", "39101=127.0.0.1:1", "--tcp", "39101=127.0.0.1:2"])
        self.assert_usage_failure(
            ["--http", "39102=127.0.0.1:1", "--tcp", "39102=127.0.0.1:2", "--key-file", self.key_file()]
        )

    def test_http_route_requires_key_file(self):
        self.assert_usage_failure(["--http", "39103=127.0.0.1:1"])

    def test_empty_or_missing_key_file(self):
        self.assert_usage_failure(["--http", "39104=127.0.0.1:1", "--key-file", self.key_file(" \n")])
        self.assert_usage_failure(["--http", "39104=127.0.0.1:1", "--key-file", str(self.dir / "absent")])

    def test_invalid_route_spec(self):
        self.assert_usage_failure(["--tcp", "not-a-route"])
        self.assert_usage_failure(["--tcp", "0=127.0.0.1:1"])

    def test_bind_failure_exits_2_without_leaking_key(self):
        with socket.socket() as occupier:
            occupier.bind(("127.0.0.1", 0))
            occupier.listen(1)
            port = occupier.getsockname()[1]
            self.assert_usage_failure(
                ["--bind", "127.0.0.1", "--http", "%d=127.0.0.1:9" % port, "--key-file", self.key_file()]
            )


if __name__ == "__main__":
    unittest.main()
