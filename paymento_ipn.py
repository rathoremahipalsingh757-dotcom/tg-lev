"""Authenticated HTTP receiver for Paymento IPN notifications."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
MAX_CALLBACK_BYTES = 64 * 1024


class PaymentoIPNServer:
    def __init__(
        self,
        host: str,
        port: int,
        public_url: str,
        secret: str,
        loop: asyncio.AbstractEventLoop,
        wake_event: asyncio.Event,
    ) -> None:
        self._path = urlsplit(public_url).path
        self._secret = secret.encode("utf-8")
        self._loop = loop
        self._wake_event = wake_event
        self._server = ThreadingHTTPServer(
            (host, port), self._handler()
        )
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="paymento-ipn",
            daemon=True,
        )

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        expected_path = self._path
        secret = self._secret
        loop = self._loop
        wake_event = self._wake_event

        class RequestHandler(BaseHTTPRequestHandler):
            def _send_health_response(self, include_body: bool) -> None:
                response = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                if include_body:
                    self.wfile.write(response)

            def do_POST(self) -> None:
                if self.path != expected_path:
                    self.send_error(404)
                    return

                raw_length = self.headers.get("Content-Length", "")
                try:
                    content_length = int(raw_length)
                except ValueError:
                    self.send_error(400, "Invalid Content-Length")
                    return
                if not 0 < content_length <= MAX_CALLBACK_BYTES:
                    self.send_error(413 if content_length > MAX_CALLBACK_BYTES else 400)
                    return

                raw_body = self.rfile.read(content_length)
                if len(raw_body) != content_length:
                    self.send_error(400, "Incomplete request body")
                    return

                signature = (
                    self.headers.get("X-HMAC-SHA256-SIGNATURE")
                    or self.headers.get("HMAC_SHA256_SIGNATURE")
                    or ""
                )
                expected_signature = hmac.new(
                    secret, raw_body, hashlib.sha256
                ).hexdigest().upper()
                if not signature or not hmac.compare_digest(
                    signature.upper(), expected_signature
                ):
                    self.send_error(401, "Invalid Paymento signature")
                    return

                try:
                    payload = json.loads(raw_body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self.send_error(400, "Invalid JSON")
                    return
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("Token"), str)
                    or not payload["Token"]
                    or not isinstance(payload.get("OrderId"), str)
                    or not payload["OrderId"]
                ):
                    self.send_error(400, "Missing payment identifiers")
                    return

                try:
                    loop.call_soon_threadsafe(wake_event.set)
                except RuntimeError:
                    logger.exception("Could not signal the Paymento IPN to the bot.")
                    self.send_error(503, "Payment receiver is shutting down")
                    return

                response = json.dumps({"received": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def do_GET(self) -> None:
                if self.path == "/health":
                    self._send_health_response(include_body=True)
                    return
                self.send_error(404)

            def do_HEAD(self) -> None:
                if self.path == "/health":
                    self._send_health_response(include_body=False)
                    return
                self.send_error(404)

            def log_message(self, format_string: str, *args: Any) -> None:
                logger.info("Paymento IPN request: " + format_string, *args)

        return RequestHandler

    def start(self) -> None:
        self._thread.start()
        logger.info(
            "Paymento IPN receiver listening on %s:%s%s",
            *self._server.server_address[:2],
            self._path,
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def stop(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
            self._thread.join(timeout=5)
        self._server.server_close()
