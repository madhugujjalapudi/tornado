import asyncio
import ssl
import os
import socket
import threading
from urllib.parse import urlparse
from typing import cast
import tornado.websocket
from tornado.websocket import WebSocketClientConnection
from tornado.netutil import Resolver
from tornado.iostream import IOStream
from tornado.httpclient import _RequestProxy

_original_ws_connect = tornado.websocket.websocket_connect


class ProxyTunnelResolver(Resolver):

    def initialize(self, proxy_host, proxy_port, target_host, target_port):
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.target_host = target_host
        self.target_port = target_port
        self._iostream = None

    async def resolve(self, host, port, family=socket.AF_UNSPEC):

        loop = asyncio.get_event_loop()

        # Step 1 — Create raw socket directly, no asyncio transports involved
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.setblocking(True)  # blocking for the setup phase

        # Step 2 — Connect to proxy (blocking, simple, no asyncio)
        raw_sock.connect((self.proxy_host, self.proxy_port))

        # Step 3 — Send HTTP CONNECT (blocking)
        connect_req = (
            f"CONNECT {self.target_host}:{self.target_port} HTTP/1.1\r\n"
            f"Host: {self.target_host}:{self.target_port}\r\n"
            f"Proxy-Connection: keep-alive\r\n\r\n"
        )
        raw_sock.sendall(connect_req.encode())

        # Step 4 — Read proxy response (blocking)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = raw_sock.recv(4096)
            if not chunk:
                raise Exception("Proxy closed connection during CONNECT")
            response += chunk

        if b"200" not in response:
            raw_sock.close()
            raise Exception(f"Proxy CONNECT failed: {response}")

        # Step 5 — SSL handshake directly on raw socket (blocking, no asyncio)
        ssl_ctx = ssl.create_default_context()
        ssl_sock = ssl_ctx.wrap_socket(
            raw_sock,
            server_hostname=self.target_host,
            do_handshake_on_connect=True  # completes fully before returning
        )

        # Step 6 — Switch to non-blocking for Tornado
        ssl_sock.setblocking(False)

        # Step 7 — Wrap in plain IOStream (SSL already done, Tornado sees ws://)
        self._iostream = IOStream(ssl_sock)

        return [(socket.AF_INET, (self.target_host, self.target_port))]


class ProxiedWebSocketClientConnection(WebSocketClientConnection):

    def __init__(self, request, proxy_resolver=None, **kwargs):
        self._proxy_resolver = proxy_resolver
        super().__init__(request, **kwargs)

        # Patch tcp_client.connect to return our pre-built stream
        if proxy_resolver and proxy_resolver._iostream:
            _stream = proxy_resolver._iostream
            async def _patched_connect(host, port, *a, **kw):
                return _stream
            self.tcp_client.connect = _patched_connect




def _build_tunnel_sync(resolver):
    result = {}
    exception = {}

    def _run():
        try:
            # Plain TCP to proxy
            raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw_sock.setblocking(True)
            raw_sock.settimeout(30)
            raw_sock.connect((resolver.proxy_host, resolver.proxy_port))

            # HTTP CONNECT
            connect_req = (
                f"CONNECT {resolver.target_host}:{resolver.target_port} HTTP/1.1\r\n"
                f"Host: {resolver.target_host}:{resolver.target_port}\r\n"
                f"Proxy-Connection: keep-alive\r\n\r\n"
            )
            raw_sock.sendall(connect_req.encode())

            # Read CONNECT response
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = raw_sock.recv(4096)
                if not chunk:
                    raise Exception("Proxy closed connection during CONNECT")
                response += chunk

            if b"200" not in response:
                raw_sock.close()
                raise Exception(f"Proxy CONNECT failed: {response}")

            # SSL directly to GCP through tunnel
            ssl_ctx = ssl.create_default_context()
            ssl_sock = ssl_ctx.wrap_socket(
                raw_sock,
                server_hostname=resolver.target_host,
                do_handshake_on_connect=True
            )
            ssl_sock.setblocking(False)
            result['sock'] = ssl_sock

        except Exception as e:
            exception['error'] = e

    # Run in a dedicated thread — completely isolated from asyncio event loop
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=35)

    if exception:
        raise exception['error']
    if 'sock' not in result:
        raise Exception("Tunnel build timed out")

    resolver._ssl_sock = result['sock']
    
from tornado.httpclient import HTTPRequest
from tornado import httputil

async def _proxy_ws_connect(url, *args, **kwargs):
    url_str = url.url if hasattr(url, "url") else url

    if "googleusercontent.com" not in url_str:
        return await _original_ws_connect(url, *args, **kwargs)

    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if not proxy_url:
        return await _original_ws_connect(url, *args, **kwargs)

    parsed_proxy = urlparse(proxy_url)
    parsed_target = urlparse(url_str)

    resolver = ProxyTunnelResolver(
        proxy_host=parsed_proxy.hostname,
        proxy_port=parsed_proxy.port,
        target_host=parsed_target.hostname,
        target_port=parsed_target.port or 443,
    )

    # Dedicated thread — fully isolated from asyncio
    _build_tunnel_sync(resolver)

    resolver._iostream = IOStream(resolver._ssl_sock)

    if isinstance(url, str):
        url = url.replace("wss://", "ws://", 1)
        request = HTTPRequest(url)
    elif hasattr(url, "url"):
        url.url = url.url.replace("wss://", "ws://", 1)
        request = url

    request.headers = httputil.HTTPHeaders(request.headers)
    request = _RequestProxy(request, HTTPRequest._DEFAULTS)

    conn = ProxiedWebSocketClientConnection(
        request,
        proxy_resolver=resolver,
        **kwargs
    )
    return await conn.connect_future
def _patched_ws_connect(url, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return asyncio.ensure_future(
        _proxy_ws_connect(url, *args, **kwargs),
        loop=loop
    )


tornado.websocket.websocket_connect = _patched_ws_connect


# import socket, ssl

# # Plain TCP to proxy — no SSL
# s = socket.create_connection(('YOUR_PROXY_HOST', YOUR_PROXY_PORT))

# # CONNECT through plain proxy
# s.sendall(
#     b"CONNECT <project>-dot-us-central1.kernels.googleusercontent.com:443 HTTP/1.1\r\n"
#     b"Host: <project>-dot-us-central1.kernels.googleusercontent.com:443\r\n\r\n"
# )
# print(s.recv(4096))

# # NOW SSL — directly to GCP, through the tunnel
# ctx = ssl.create_default_context()
# ctx.check_hostname = False
# ctx.verify_mode = ssl.CERT_NONE
# ss = ctx.wrap_socket(s, server_hostname="<project>-dot-us-central1.kernels.googleusercontent.com")
# print("Issuer:", ss.getpeercert())
