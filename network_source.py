"""Read grouped Quest Pro grayscale frames from an MJPEG HTTP stream."""

from __future__ import annotations

import http.client
import time
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import numpy as np

from capture_format import TRANSPORT_HEADER


_MAGIC = b"QPLIVE3\0"
_MODE_LAYOUTS = {
    "all": (2000, 0x1F),
    "face": (1200, 0x1C),
    "mouth": (800, 0x0C),
    "eyes": (800, 0x03),
}
_MODE_PATHS = {
    "all": "/strip.mjpg",
    "face": "/face.mjpg",
    "mouth": "/mouth.mjpg",
    "eyes": "/eyes.mjpg",
}
DEFAULT_PORT = 27280


class NetworkFrameSource:
    def __init__(self, base_url: str, mode: str, timeout_s: float = 5.0) -> None:
        if mode not in _MODE_LAYOUTS:
            raise ValueError(f"Unsupported network camera mode: {mode}")
        base_url = base_url.strip()
        if "://" not in base_url:
            # Accept a bare "192.168.0.140" or "192.168.0.140:27280" as typed into the Hub.
            base_url = "http://" + base_url
        parsed = urlsplit(base_url)
        if parsed.hostname and parsed.port is None and parsed.scheme == "http":
            base_url = f"http://{parsed.hostname}:{DEFAULT_PORT}{parsed.path}"
            parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("base_url must be an HTTP or HTTPS URL")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port
        self._base_path = parsed.path.rstrip("/")
        self._mode = mode
        self._width, self._camera_mask = _MODE_LAYOUTS[mode]
        self._timeout_s = timeout_s
        self._connection: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
        self._response: http.client.HTTPResponse | None = None
        self._boundary: bytes | None = None
        self._fallback_sequence = 0

    def connect(self, deadline_s: float = 20.0, stop_file: Path | None = None) -> None:
        self.close()
        deadline = time.monotonic() + deadline_s
        while True:
            if stop_file is not None and stop_file.exists():
                return
            connection = None
            try:
                connection_type = (
                    http.client.HTTPSConnection
                    if self._scheme == "https"
                    else http.client.HTTPConnection
                )
                connection = connection_type(self._host, self._port, timeout=self._timeout_s)
                path = f"{self._base_path}{_MODE_PATHS[self._mode]}"
                connection.request("GET", path, headers={"Accept": "multipart/x-mixed-replace"})
                response = connection.getresponse()
                if response.status != 200:
                    raise ConnectionError(f"MJPEG endpoint returned HTTP {response.status}")
                boundary = self._parse_boundary(response.getheader("Content-Type", ""))
                self._connection = connection
                self._response = response
                self._boundary = boundary
                self._fallback_sequence = 0
                return
            except (OSError, http.client.HTTPException, ConnectionError) as error:
                if connection is not None:
                    connection.close()
                if time.monotonic() >= deadline:
                    raise ConnectionError(f"Could not connect to MJPEG stream: {error}") from error
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _parse_boundary(content_type: str) -> bytes:
        for parameter in content_type.split(";")[1:]:
            key, separator, value = parameter.strip().partition("=")
            if separator and key.lower() == "boundary":
                boundary = value.strip().strip('"').encode("ascii")
                if boundary:
                    return boundary if boundary.startswith(b"--") else b"--" + boundary
        raise ConnectionError("MJPEG response has no multipart boundary")

    def _readline(self) -> bytes:
        assert self._response is not None
        line = self._response.readline()
        if not line:
            raise ConnectionError("MJPEG stream disconnected")
        return line.rstrip(b"\r\n")

    def read_frame(self) -> tuple[bytes, bytes]:
        if self._response is None or self._boundary is None:
            raise ConnectionError("Network frame source is not connected")
        try:
            line = self._readline()
            while line != self._boundary:
                if line == self._boundary + b"--":
                    raise ConnectionError("MJPEG stream ended")
                line = self._readline()

            headers: dict[bytes, bytes] = {}
            while True:
                line = self._readline()
                if not line:
                    break
                name, separator, value = line.partition(b":")
                if separator:
                    headers[name.strip().lower()] = value.strip()
            if b"content-length" not in headers:
                raise ConnectionError("MJPEG part is missing Content-Length")
            try:
                jpeg_size = int(headers[b"content-length"])
            except ValueError as error:
                raise ConnectionError("MJPEG part has an invalid Content-Length") from error
            if jpeg_size < 0:
                raise ConnectionError("MJPEG part has an invalid Content-Length")
            jpeg = self._response.read(jpeg_size)
            if len(jpeg) != jpeg_size:
                raise ConnectionError("MJPEG stream disconnected during a frame")
            trailer = self._response.read(2)
            if trailer != b"\r\n":
                raise ConnectionError("Malformed MJPEG part separator")

            image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if image is None or image.shape != (400, self._width):
                actual = None if image is None else f"{image.shape[1]}x{image.shape[0]}"
                raise ValueError(
                    f"Decoded frame size {actual} does not match {self._width}x400 mode"
                )
            sequence = self._header_integer(headers, b"x-sequence")
            if sequence is None:
                self._fallback_sequence += 1
                sequence = self._fallback_sequence
            timestamp_ns = self._header_integer(headers, b"x-timestamp-ns")
            if timestamp_ns is None:
                timestamp_ns = time.monotonic_ns()
            payload = image.tobytes(order="C")
            raw_header = TRANSPORT_HEADER.pack(
                _MAGIC, 3, TRANSPORT_HEADER.size, sequence, timestamp_ns,
                self._width, 400, self._width, 1, len(payload), self._camera_mask, 0,
            )
            return raw_header, payload
        except (OSError, http.client.HTTPException) as error:
            raise ConnectionError("MJPEG stream disconnected") from error

    @staticmethod
    def _header_integer(headers: dict[bytes, bytes], key: bytes) -> int | None:
        value = headers.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError as error:
            raise ConnectionError(f"MJPEG part has invalid {key.decode('ascii')} header") from error

    def close(self) -> None:
        if self._response is not None:
            self._response.close()
        if self._connection is not None:
            self._connection.close()
        self._response = None
        self._connection = None
        self._boundary = None
