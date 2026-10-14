"""Verify per-connection routing with overlapping real loopback tunnels."""
from concurrent.futures import ThreadPoolExecutor
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from pathlib import Path

import network_model
import network_probe
from network_transport import Cancellation, read_headers
from test_network import FakeController


class PinnedProxy(socketserver.BaseRequestHandler):
    """Simulate a proxy that selects an outbound on receiving its first TLS bytes."""

    def handle(self):
        """Echo the pinned outbound after admission, even when the selector changes."""
        self.request.settimeout(3)
        try:
            data = bytearray()
            while not data.endswith(b"\r\n\r\n"):
                chunk = self.request.recv(1)
                if not chunk:
                    return
                data.extend(chunk)
            self.request.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
            if not self.request.recv(64):
                return
            node = self.server.controller.current
            self.server.routes[self.client_address[1]] = node
            if node == "a":
                self.server.a_connected.set()
                # A deliberately stalls TLS until B is already attributed. Holding
                # the selector through TLS would deadlock this controlled exchange.
                if not self.server.release_a.wait(2):
                    return
            self.request.sendall(b"server-hello")
            while self.request.recv(64):
                self.request.sendall(node.encode())
        except OSError:
            pass


class ConcurrentRouteTests(unittest.TestCase):
    """New and retried tunnels must bind correctly while existing streams overlap."""

    def test_parallel_streams_and_reconnect_keep_their_own_node(self):
        """A/B traffic overlaps and A reconnects without inheriting B's current route."""
        with tempfile.TemporaryDirectory() as directory:
            controller = FakeController()
            proxy = socketserver.ThreadingTCPServer(("127.0.0.1", 0), PinnedProxy)
            proxy.controller, proxy.routes = controller, {}
            proxy.a_connected, proxy.release_a = threading.Event(), threading.Event()
            thread = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": .01})
            thread.start()
            settings = {"controller": "http://127.0.0.1:9090", "group": "group"}
            lease = network_probe.RouteLease(settings, Path(directory))
            journal = {"group": "group", "original": "original", "owned": "original", "pending": None}
            routes = network_probe.RouteBinding(controller, lease, journal, Cancellation())
            verified = []

            def verify(port, node, group):
                """Attribute by source port rather than the changing current selector."""
                self.assertEqual(proxy.routes[port], node)
                self.assertEqual(group, "group")
                verified.append(node)

            controller.verify_route = verify
            relays, sockets = [], []
            try:
                for node in ("a", "b"):
                    relay = network_model.Relay(f"http://127.0.0.1:{proxy.server_address[1]}",
                        controller, node, "group", routes.cancel, 6,
                        route_guard=lambda node=node, **kwargs: routes.bind(node, **kwargs))
                    relays.append(relay)

                def connect(relay):
                    """Complete fake TLS admission through the actual encrypted-stream relay."""
                    sock = socket.create_connection(relay.server_address, timeout=2)
                    sockets.append(sock)
                    sock.sendall(b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n")
                    self.assertEqual(read_headers(sock, time.monotonic() + 2)[0], 200)
                    sock.sendall(b"client-hello")
                    self.assertEqual(sock.recv(64), b"server-hello")
                    return sock

                barrier = threading.Barrier(2)

                def stream(relay):
                    """Require both nodes to be admitted before either finishes its response."""
                    if relay.node == "b":
                        self.assertTrue(proxy.a_connected.wait(1))
                    sock = connect(relay)
                    if relay.node == "b":
                        proxy.release_a.set()
                    barrier.wait(2)
                    sock.sendall(b"authenticated-application-data")
                    self.assertEqual(sock.recv(64), relay.node.encode())
                    return sock

                with ThreadPoolExecutor(max_workers=2) as pool:
                    active = list(pool.map(stream, relays))
                # Force B to be current, then reconnect A. Checking only at the
                # start of a model sample would route this new connection to B.
                with routes.bind("b"):
                    pass
                again = connect(relays[0])
                again.sendall(b"retry-data")
                self.assertEqual(again.recv(64), b"a")
                for relay, sock in zip(relays, active):
                    sock.sendall(b"still-pinned")
                    self.assertEqual(sock.recv(64), relay.node.encode())
                self.assertCountEqual(verified, ["a", "b", "a"])
                routes.verify()
            finally:
                proxy.release_a.set()
                for sock in sockets:
                    sock.close()
                for relay in relays:
                    relay.close()
                lease.restore(controller)
                lease.close()
                proxy.shutdown()
                proxy.server_close()
                thread.join(2)
            self.assertEqual(controller.current, "original")
