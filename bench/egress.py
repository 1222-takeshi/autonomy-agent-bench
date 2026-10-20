"""Egress gateway: forward fixed listen ports to fixed upstreams and inject a key.

The agent under test sits on an internal Docker network with no route out.
This gateway lives on that network and on the default bridge. Each listen port
forwards to exactly one upstream, and for plain-HTTP routes it swaps a dummy
credential for the real one read from a key file before forwarding. Nothing else
is reachable. Standard library only.
"""
import argparse
import re
import selectors
import ssl
import signal
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

DEFAULT_IDLE_TIMEOUT = 600
DEFAULT_MAX_HEAD_BYTES = 65536
DEFAULT_MAX_CONNECTIONS = 64
HOP_HEADERS = frozenset(("connection", "proxy-connection", "keep-alive",
                         "proxy-authorization", "te", "upgrade"))
AUTH_HEADERS = frozenset(("authorization", "x-api-key"))
AUTH_STYLES = ("both", "x-api-key", "bearer")
CHUNK_SIZE = 65536
CONNECT_TIMEOUT = 30
ALLOWED_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE",
                             "OPTIONS"})

HOST_RE = re.compile(r"[A-Za-z0-9.-]+")
# RFC 9110 field-name token: no whitespace allowed at all.
TOKEN_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")

_BAD_PREFIX = "HTTP/1.1 %s\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
BAD_REQUEST = (_BAD_PREFIX % "400 Bad Request").encode("latin-1")
BAD_GATEWAY = (_BAD_PREFIX % "502 Bad Gateway").encode("latin-1")


class UsageError(Exception):
    """The gateway itself was invoked badly: bad argv, key, or listen port."""


def parse_route(spec):
    """Split ``LISTEN=HOST:PORT`` into ``(listen_port, host, port)``.

    Raises ``UsageError`` naming ``spec`` unless the listen and upstream ports
    are ints in 1..65535 and the host is a non-empty ``[A-Za-z0-9.-]+`` string.
    """
    match = re.fullmatch(r"(\d+)=(.+)", spec)
    if not match:
        raise UsageError("invalid route: " + spec)
    listen, rest = match.group(1), match.group(2)
    host, _, port = rest.rpartition(":")
    if not host or not HOST_RE.fullmatch(host):
        raise UsageError("invalid route: " + spec)
    try:
        listen_port = int(listen)
        upstream_port = int(port)
    except ValueError:
        raise UsageError("invalid route: " + spec)
    if not (1 <= listen_port <= 65535) or not (1 <= upstream_port <= 65535):
        raise UsageError("invalid route: " + spec)
    return (listen_port, host, upstream_port)


def read_key(path):
    """Return the stripped key text read from ``path``.

    Raises ``UsageError("key file missing or empty")`` on any read failure or
    content that is empty after stripping; the key text never appears in a
    message.
    """
    try:
        text = Path(path).read_text(encoding="latin-1")
    except (OSError, ValueError):
        raise UsageError("key file missing or empty")
    stripped = text.strip()
    if not stripped:
        raise UsageError("key file missing or empty")
    return stripped


def read_head(sock, max_bytes):
    """Return ``(head, rest)`` for ``sock`` up to and including the terminator.

    ``head`` keeps the terminator; ``rest`` holds any bytes read past it.
    Raises ``ValueError("head too large")`` past ``max_bytes`` without the
    terminator, or ``ValueError("connection closed")`` when the peer closes.
    """
    buffer = b""
    while True:
        if len(buffer) > max_bytes:
            raise ValueError("head too large")
        cut = buffer.find(b"\r\n\r\n")
        if cut != -1:
            return buffer[:cut + 4], buffer[cut + 4:]
        try:
            data = sock.recv(CHUNK_SIZE)
        except OSError:
            raise ValueError("connection closed")
        if not data:
            raise ValueError("connection closed")
        buffer += data


def rewrite_head(head, key, upstream_host, upstream_port,
                 auth_style="both", default_port=None):
    """Return the request head rewritten to reach ``upstream_host:upstream_port``.

    Validates the request line, drops hop and auth headers, resets ``Host`` (all
    cases, all counts), and appends ``Host``, the auth header(s) the style asks
    for (``Authorization`` before ``x-api-key``) and ``Connection: close``. Other
    headers keep their original order and case. ``default_port`` suppresses the
    ``:port`` on ``Host`` when it equals ``upstream_port`` (the HTTPS case).
    Raises ``ValueError`` on a bad request line, header, obs-fold continuation,
    or an unknown ``auth_style``.
    """
    if auth_style not in AUTH_STYLES:
        raise ValueError("unknown auth style: %r" % (auth_style,))
    text = head.decode("latin-1")
    cut = text.find("\r\n\r\n")
    if cut == -1:
        text = text + "\r\n\r\n"
        cut = len(text) - 4
    request_line, rest = text[:cut].split("\r\n", 1)
    request_line, rest_lines = request_line, rest.split("\r\n")
    parts = request_line.split(" ")
    if parts[0] not in ALLOWED_METHODS:
        raise ValueError("method not allowed")
    if (len(parts) != 3 or not parts[2].startswith("HTTP/1.")
            or not parts[1].startswith("/")):
        raise ValueError("bad request line")
    headers = []
    for raw in rest_lines:
        if raw[:1] in (" ", "\t"):
            raise ValueError("bad header")
        if ":" not in raw:
            raise ValueError("bad header")
        name = raw.split(":", 1)[0]
        if not TOKEN_RE.fullmatch(name):
            raise ValueError("bad header")
        lower = name.lower()
        if lower in HOP_HEADERS or lower in AUTH_HEADERS or lower == "host":
            continue
        headers.append(raw)
    output = [request_line]
    output.extend(headers)
    if default_port is not None and default_port == upstream_port:
        output.append("Host: %s" % upstream_host)
    else:
        output.append("Host: %s:%d" % (upstream_host, upstream_port))
    if auth_style in ("bearer", "both"):
        output.append("Authorization: Bearer %s" % key)
    if auth_style in ("x-api-key", "both"):
        output.append("x-api-key: %s" % key)
    output.append("Connection: close")
    return ("\r\n".join(output) + "\r\n\r\n").encode("latin-1")


def tls_connector(ca_file=None):
    """Return ``connect(address, timeout)`` that TLS-wraps a fresh socket.

    ``ssl.create_default_context`` keeps hostname and certificate verification
    enabled, so a self-signed or mismatched upstream raises ``ssl.SSLError`` (an
    ``OSError`` subclass) that ``handle_http`` answers with a 502.
    """
    context = ssl.create_default_context(cafile=ca_file)

    def connect(address, timeout):
        sock = socket.create_connection(address, timeout=timeout)
        return context.wrap_socket(sock, server_hostname=address[0])

    return connect


def relay(a, b, idle_timeout):
    """Copy bytes both ways until both EOF or the idle timeout elapses.

    When one side reaches EOF, ``shutdown(SHUT_WR)`` is issued on the peer so a
    half-close propagates; that side keeps reading until the peer replies. Both
    sockets are closed before returning, and peer errors never propagate.
    """
    selector = selectors.DefaultSelector()
    peers = {a.fileno(): b, b.fileno(): a}

    def service(conn):
        peer = peers.get(conn.fileno())
        try:
            buffer = conn.recv(CHUNK_SIZE)
        except OSError:
            return b""
        if not buffer:
            if peer is not None:
                try:
                    peer.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
            return b""
        try:
            peer.sendall(buffer)
        except OSError:
            pass
        return buffer

    regs = [(conn, selector.register(conn, selectors.EVENT_READ, service))
            for conn in (a, b)]
    try:
        while regs:
            events = selector.select(timeout=idle_timeout)
            if not events:
                break
            ready = {key.fileobj for key, _ in events}
            still = []
            for conn, reg in regs:
                if conn in ready:
                    moved = service(conn)
                    if moved:
                        still.append((conn, reg))
                    else:
                        _deregister(selector, reg)
                else:
                    still.append((conn, reg))
            regs = still
    finally:
        for conn in (a, b):
            try:
                conn.close()
            except OSError:
                pass


def _deregister(selector, reg):
    try:
        selector.unregister(reg)
    except Exception:  # noqa: BLE001 - a deregister must never escape relay
        pass


def _respond(sock, body):
    try:
        sock.sendall(body)
    except OSError:
        pass


def _safe_close(sock):
    try:
        sock.close()
    except OSError:
        pass


def _write_log(line):
    """Append ``line`` to stderr; a write error must never escape the handler."""
    try:
        sys.stderr.write(line)
    except OSError:
        pass


def _reject_client(client):
    """Drop a client that arrived with no free slot.

    Tears the connection down via ``SHUT_RDWR`` then close. Nothing is read and
    no response is sent, yet the peer observes a clean EOF rather than a reset
    (closing with unread data would otherwise send a RST).
    """
    try:
        client.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    _safe_close(client)


def handle_http(client, upstream, key, idle_timeout, max_head_bytes,
                connect=socket.create_connection, auth_style="both",
                default_port=None, log_prefix="http"):
    """Read one request head, rewrite it, connect upstream, and relay the swap."""
    try:
        head, rest = read_head(client, max_head_bytes)
        request_line = head.split(b"\r\n", 1)[0]
        parts = request_line.split(b" ")
        method = path = ""
        if len(parts) == 3:
            method = parts[0].decode("latin-1")
            path = parts[1].split(b"?", 1)[0].decode("latin-1")
        head = rewrite_head(head, key, upstream[0], upstream[1],
                            auth_style=auth_style, default_port=default_port)
    except ValueError:
        _respond(client, BAD_REQUEST)
        client.close()
        return
    try:
        upstream_sock = connect(upstream, CONNECT_TIMEOUT)
    except OSError:
        _respond(client, BAD_GATEWAY)
        client.close()
        return
    try:
        upstream_sock.settimeout(idle_timeout)
    except OSError:
        pass
    try:
        upstream_sock.sendall(head + rest)
    except OSError:
        _safe_close(upstream_sock)
        _safe_close(client)
        return
    try:
        listen_port = client.getsockname()[1]
    except OSError:
        listen_port = 0
    try:
        relay(client, upstream_sock, idle_timeout)
    except Exception:  # noqa: BLE001 - a relay failure must not break the loop
        pass
    _safe_close(upstream_sock)
    _safe_close(client)
    _write_log("%s %d %s %s -> %s:%d\n"
               % (log_prefix, listen_port, method, path, upstream[0], upstream[1]))


def handle_tcp(client, upstream, idle_timeout,
               connect=socket.create_connection):
    """Connect upstream and relay raw bytes both ways; log the route."""
    try:
        upstream_sock = connect(upstream, CONNECT_TIMEOUT)
    except OSError:
        client.close()
        return
    try:
        upstream_sock.settimeout(idle_timeout)
    except OSError:
        pass
    try:
        listen_port = client.getsockname()[1]
    except OSError:
        listen_port = 0
    try:
        relay(client, upstream_sock, idle_timeout)
    except Exception:  # noqa: BLE001
        pass
    _safe_close(upstream_sock)
    _safe_close(client)
    _write_log("tcp %d -> %s:%d\n"
               % (listen_port, upstream[0], upstream[1]))


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _serve(bind, route, http_route, key, idle_timeout, max_head_bytes,
           max_connections, connect, auth_style, default_port, log_prefix):
    listen_port, host, port = route
    upstream = (host, port)
    semaphore = threading.BoundedSemaphore(max_connections)

    class _Handler(socketserver.BaseRequestHandler):
        def handle(self):
            client = self.request
            if not semaphore.acquire(blocking=False):
                _reject_client(client)
                return
            try:
                client.settimeout(30)
                if self.server.route_kind == "http":
                    handle_http(client, upstream, key, idle_timeout, max_head_bytes,
                                connect=connect, auth_style=auth_style,
                                default_port=default_port, log_prefix=log_prefix)
                else:
                    handle_tcp(client, upstream, idle_timeout)
            finally:
                semaphore.release()

    server = _Server((bind, listen_port), _Handler)
    server.route_kind = "http" if http_route else "tcp"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def start(bind, http_routes, tcp_routes, key, idle_timeout, max_head_bytes,
          max_connections=DEFAULT_MAX_CONNECTIONS,
          https_routes=(), auth_style="both", ca_file=None):
    """Build and start one threaded server per route, returning the servers.

    Each route is ``(listen_port, host, port)``; ``http_routes``,
    ``https_routes`` and ``tcp_routes`` choose the handler. HTTPS routes
    connect with a TLS socket built from ``ca_file`` and log with an ``https``
    prefix. A listen port of ``0`` lets the OS pick the port. Each server runs
    in a daemon thread and accepts at most ``max_connections`` simultaneous
    clients.
    """
    https_connect = tls_connector(ca_file) if https_routes else None
    servers = []
    for route in list(http_routes):
        servers.append(_serve(bind, route, True, key, idle_timeout, max_head_bytes,
                              max_connections, socket.create_connection,
                              auth_style, None, "http"))
    for route in list(https_routes):
        servers.append(_serve(bind, route, True, key, idle_timeout, max_head_bytes,
                              max_connections, https_connect,
                              auth_style, 443, "https"))
    for route in list(tcp_routes):
        servers.append(_serve(bind, route, False, key, idle_timeout, max_head_bytes,
                              max_connections, socket.create_connection,
                              auth_style, None, "http"))
    return servers


def _ready_line(servers):
    ports = sorted(server.server_address[1] for server in servers)
    return "ready " + " ".join(str(port) for port in ports)


def main(argv=None):
    """Parse argv, start the servers, print ``ready``, and serve until signalled."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--http", action="append", default=[],
                        metavar="LISTEN=HOST:PORT")
    parser.add_argument("--https", action="append", default=[],
                        metavar="LISTEN=HOST:PORT")
    parser.add_argument("--tcp", action="append", default=[],
                        metavar="LISTEN=HOST:PORT")
    parser.add_argument("--auth-style", choices=AUTH_STYLES, default="both")
    parser.add_argument("--ca-file", default=None)
    parser.add_argument("--key-file", default=None)
    parser.add_argument("--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT)
    parser.add_argument("--max-head-bytes", type=int, default=DEFAULT_MAX_HEAD_BYTES)
    parser.add_argument("--max-connections", type=int, default=DEFAULT_MAX_CONNECTIONS)
    args = parser.parse_args(argv)
    try:
        http_routes = []
        https_routes = []
        tcp_routes = []
        ports = set()
        for spec in args.http:
            parsed = parse_route(spec)
            if parsed[0] in ports:
                raise UsageError("duplicate listen port: %d" % parsed[0])
            ports.add(parsed[0])
            http_routes.append(parsed)
        for spec in args.https:
            parsed = parse_route(spec)
            if parsed[0] in ports:
                raise UsageError("duplicate listen port: %d" % parsed[0])
            ports.add(parsed[0])
            https_routes.append(parsed)
        for spec in args.tcp:
            parsed = parse_route(spec)
            if parsed[0] in ports:
                raise UsageError("duplicate listen port: %d" % parsed[0])
            ports.add(parsed[0])
            tcp_routes.append(parsed)
        if not http_routes and not https_routes and not tcp_routes:
            raise UsageError("at least one --http, --https or --tcp route is required")
        if (http_routes or https_routes) and not args.key_file:
            raise UsageError("--key-file is required when an --http or --https route exists")
        key = read_key(args.key_file)
    except UsageError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2
    try:
        servers = start(args.bind, http_routes, tcp_routes, key,
                        args.idle_timeout, args.max_head_bytes,
                        args.max_connections, https_routes=https_routes,
                        auth_style=args.auth_style, ca_file=args.ca_file)
    except OSError as exc:
        sys.stderr.write("bind failed: %s\n" % exc)
        return 2
    sys.stdout.write(_ready_line(servers) + "\n")
    sys.stdout.flush()
    stop = threading.Event()

    def _handle(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    try:
        while not stop.is_set():
            stop.wait(0.2)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
