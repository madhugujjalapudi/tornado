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


class ProxyTunnelResolver(Resolver):
    """
    Custom Tornado Resolver that tunnels through an HTTP CONNECT proxy.
    Injected into TCPClient inside WebSocketClientConnection.
    """

    def initialize(self, proxy_host, proxy_port, target_host, target_port):
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.target_host = target_host
        self.target_port = target_port

    async def resolve(self, host, port, family=socket.AF_UNSPEC):
        # Step 1 — TCP connect to proxy via asyncio
        reader, writer = await asyncio.open_connection(
            self.proxy_host, self.proxy_port
        )

        # Step 2 — HTTP CONNECT tunnel request
        connect_req = (
            f"CONNECT {self.target_host}:{self.target_port} HTTP/1.1\r\n"
            f"Host: {self.target_host}:{self.target_port}\r\n"
            f"Proxy-Connection: keep-alive\r\n\r\n"
        )
        writer.write(connect_req.encode())
        await writer.drain()

        # Step 3 — Read proxy response
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = await reader.read(4096)
            if not chunk:
                raise Exception("Proxy closed connection before CONNECT response")
            response += chunk

        if b"200" not in response:
            writer.close()
            raise Exception(f"Proxy CONNECT failed: {response}")

        # Step 4 — SSL upgrade using asyncio loop.start_tls (safe on Windows)
        ssl_ctx = ssl.create_default_context()
        transport = writer.transport
        protocol = transport.get_protocol()
        loop = asyncio.get_event_loop()

        ssl_transport = await loop.start_tls(
            transport,
            protocol,
            ssl_ctx,
            server_side=False,
            server_hostname=self.target_host,
        )

        # Step 5 — Return the tunneled socket in Tornado's expected format
        # resolve() must return a list of (family, address) tuples
        # We store the ssl_transport so TCPClient can grab it
        self._ssl_transport = ssl_transport
        tunneled_sock = ssl_transport.get_extra_info("socket")
        return [(socket.AF_INET, tunneled_sock.getpeername())]


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

    # Pass our resolver into websocket_connect — it flows into TCPClient
    return await _original_ws_connect(url, *args, resolver=resolver, **kwargs)


def _patched_ws_connect(url, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return asyncio.ensure_future(
        _proxy_ws_connect(url, *args, **kwargs), loop=loop
    )


tornado.websocket.websocket_connect = _patched_ws_connect
