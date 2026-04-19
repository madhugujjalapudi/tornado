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

        if proxy_resolver and proxy_resolver._iostream:
            _stream = proxy_resolver._iostream
            async def _patched_connect(host, port, af=None,
                                       ssl_options=None,
                                       max_buffer_size=None,
                                       source_ip=None,
                                       source_port=None,
                                       timeout=None):
                # Return pre-built SSLIOStream directly
                # Tornado calls connect() expecting a stream back
                return _stream
            self.tcp_client.connect = _patched_connect




def _build_tunnel_sync(resolver):
    import socket
    import ssl
    import traceback

    try:
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.setblocking(True)
        raw_sock.settimeout(30)
        raw_sock.connect((resolver.proxy_host, resolver.proxy_port))
        print(f"[tunnel] connected to proxy")

        connect_req = (
            f"CONNECT {resolver.target_host}:{resolver.target_port} HTTP/1.1\r\n"
            f"Host: {resolver.target_host}:{resolver.target_port}\r\n"
            f"Proxy-Connection: keep-alive\r\n\r\n"
        )
        raw_sock.sendall(connect_req.encode())
        print(f"[tunnel] sent CONNECT")

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = raw_sock.recv(4096)
            if not chunk:
                raise Exception("Proxy closed connection")
            response += chunk
        print(f"[tunnel] CONNECT response: {response}")

        if b"200" not in response:
            raise Exception(f"Proxy CONNECT failed: {response}")

        print(f"[tunnel] tunnel established, starting SSL")
        print(f"[tunnel] socket state before SSL: blocking={raw_sock.getblocking()}, timeout={raw_sock.gettimeout()}")

        ssl_ctx = ssl.create_default_context()
        print(f"[tunnel] ssl_ctx created")

        ssl_sock = ssl_ctx.wrap_socket(
            raw_sock,
            server_hostname=resolver.target_host,
            do_handshake_on_connect=False  # manual handshake
        )
        print(f"[tunnel] socket wrapped")

        # Manual handshake with full visibility
        import select
        for attempt in range(100):
            try:
                print(f"[tunnel] handshake attempt {attempt}")
                ssl_sock.do_handshake()
                print(f"[tunnel] handshake complete!")
                break
            except ssl.SSLWantReadError:
                print(f"[tunnel] WANT_READ, waiting...")
                select.select([ssl_sock], [], [], 5)
            except ssl.SSLWantWriteError:
                print(f"[tunnel] WANT_WRITE, waiting...")
                select.select([], [ssl_sock], [], 5)
            except Exception as e:
                print(f"[tunnel] handshake error: {type(e).__name__}: {e}")
                traceback.print_exc()
                raise
        
        ssl_sock.setblocking(False)
        resolver._ssl_sock = ssl_sock
        print(f"[tunnel] done, ssl_sock ready")

    except Exception as e:
        print(f"[tunnel] FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        raise    
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

    _build_tunnel_sync(resolver)

    # SSLIOStream — tells Tornado SSL is in place, skips handshake
    ssl_ctx = ssl.create_default_context()
    resolver._iostream = SSLIOStream(
        resolver._ssl_sock,
        ssl_options=ssl_ctx,
        server_hostname=resolver.target_host,
    )
    resolver._iostream._ssl_accepting = False

    # Keep wss:// — Tornado expects SSL stream for wss
    if isinstance(url, str):
        request = HTTPRequest(url)
    else:
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
