import asyncio
import ssl
import os
import socket
from urllib.parse import urlparse

import tornado.websocket
import tornado.tcpclient
from tornado.netutil import Resolver
from tornado.websocket import WebSocketClientConnection

_original_ws_connect = tornado.websocket.websocket_connect

class ProxiedWebSocketClientConnection(WebSocketClientConnection):

    def __init__(self, request, resolver=None, **kwargs):
        # resolver here is our ProxyTunnelResolver
        self._proxy_resolver = resolver
        super().__init__(request, resolver=resolver, **kwargs)

        if resolver and hasattr(resolver, '_iostream') and resolver._iostream:
            # Patch tcp_client.connect to return our pre-built stream
            _stream = resolver._iostream
            async def _patched_connect(host, port, *a, **kw):
                return _stream
            self.tcp_client.connect = _patched_connect
class ProxyTunnelResolver(Resolver):

    def initialize(self, proxy_host, proxy_port, target_host, target_port):
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.target_host = target_host
        self.target_port = target_port
        self._iostream = None  # store the tunneled stream here

    async def resolve(self, host, port, family=socket.AF_UNSPEC):
        # TCP connect to proxy
        reader, writer = await asyncio.open_connection(
            self.proxy_host, self.proxy_port
        )

        # HTTP CONNECT
        connect_req = (
            f"CONNECT {self.target_host}:{self.target_port} HTTP/1.1\r\n"
            f"Host: {self.target_host}:{self.target_port}\r\n"
            f"Proxy-Connection: keep-alive\r\n\r\n"
        )
        writer.write(connect_req.encode())
        await writer.drain()

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = await reader.read(4096)
            if not chunk:
                raise Exception("Proxy closed connection")
            response += chunk

        if b"200" not in response:
            writer.close()
            raise Exception(f"Proxy CONNECT failed: {response}")

        # SSL upgrade
        ssl_ctx = ssl.create_default_context()
        transport = writer.transport
        protocol = transport.get_protocol()
        loop = asyncio.get_event_loop()

        ssl_transport = await loop.start_tls(
            transport, protocol, ssl_ctx,
            server_side=False,
            server_hostname=self.target_host,
        )

        # Wrap in a Tornado IOStream so TCPClient can use it directly
        raw_sock = ssl_transport.get_extra_info("socket")
        loop.remove_reader(raw_sock.fileno())
        loop.remove_writer(raw_sock.fileno())
        ssl_transport.abort()    
        raw_sock.setblocking(False)
        
        from tornado.iostream import IOStream
        import tornado.ioloop
        self._iostream = IOStream(raw_sock)
        # Return dummy address — TCPClient will use our iostream instead
        return [(socket.AF_INET, ("127.0.0.1", self.target_port))]


async def _proxy_ws_connect(url, *args, **kwargs):
    url_str = url.url if hasattr(url, "url") else url

    if "googleusercontent.com" not in url_str:
        return await _original_ws_connect(url, *args, **kwargs)

    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if not proxy_url:
        return await _original_ws_connect(url, *args, **kwargs)

    parsed_proxy = urlparse(proxy_url)
    parsed_target = urlparse(url_str)

    # Build tunnel + SSL first
    resolver = ProxyTunnelResolver(
        proxy_host=parsed_proxy.hostname,
        proxy_port=parsed_proxy.port,
        target_host=parsed_target.hostname,
        target_port=parsed_target.port or 443,
    )
    # Pre-build the tunnel so iostream is ready before TCPClient needs it
    await resolver.resolve(parsed_target.hostname, parsed_target.port or 443)

    # Rewrite wss → ws so Tornado skips its own SSL
    if isinstance(url, str):
        url = url.replace("wss://", "ws://", 1)
    elif hasattr(url, "url"):
        url.url = url.url.replace("wss://", "ws://", 1)

    # Use our subclass which injects the pre-built stream
    from tornado.httpclient import HTTPRequest, _RequestProxy
    request = url if isinstance(url, HTTPRequest) else HTTPRequest(url)
    
    conn = ProxiedWebSocketClientConnection(
        request,
        resolver=resolver,
        **kwargs
    )
    return await conn.connect_future


def _patched_ws_connect(url, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return asyncio.ensure_future(
        _proxy_ws_connect(url, *args, **kwargs), loop=loop
    )


tornado.websocket.websocket_connect = _patched_ws_connect
