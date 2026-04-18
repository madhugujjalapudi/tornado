import asyncio
import os
import ssl
from urllib.parse import urlparse

import tornado.websocket
import websockets

_original_ws_connect = tornado.websocket.websocket_connect

class WebsocketsWrapper:
    """
    Wraps websockets library connection to look like 
    a Tornado WebSocketClientConnection
    """
    def __init__(self, ws):
        self.ws = ws
        self.close_code = None
        self.close_reason = None

    async def write_message(self, message, binary=False):
        await self.ws.send(message)

    async def read_message(self):
        try:
            return await self.ws.recv()
        except websockets.exceptions.ConnectionClosed as e:
            self.close_code = e.code
            self.close_reason = e.reason
            return None

    def close(self, code=1000, reason=""):
        asyncio.ensure_future(self.ws.close(code, reason))

    @property
    def selected_subprotocol(self):
        return self.ws.subprotocol


async def _proxy_ws_connect(url, *args, **kwargs):
    url_str = url.url if hasattr(url, 'url') else url

    if "googleusercontent.com" not in url_str:
        return await _original_ws_connect(url, *args, **kwargs)

    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

    # Extract headers from the original request
    headers = {}
    if hasattr(url, 'headers') and url.headers:
        headers = dict(url.headers)

    if proxy_url:
        proxy = urlparse(proxy_url)
        proxy_uri = f"http://{proxy.hostname}:{proxy.port}"

        ws = await websockets.connect(
            url_str,
            additional_headers=headers,
            proxy=proxy_uri,
            ssl=ssl.create_default_context()
        )
    else:
        ws = await websockets.connect(
            url_str,
            additional_headers=headers,
            ssl=ssl.create_default_context()
        )

    return WebsocketsWrapper(ws)


def _patched_ws_connect(url, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return asyncio.ensure_future(
        _proxy_ws_connect(url, *args, **kwargs),
        loop=loop
    )

tornado.websocket.websocket_connect = _patched_ws_connect