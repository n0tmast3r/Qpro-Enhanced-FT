import http.server
import socket
import threading
import unittest

import cv2
import numpy as np

from capture_format import TRANSPORT_HEADER
from network_source import NetworkFrameSource


class _MJPEGServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, frames: list[tuple[np.ndarray, int, int]], close_after: bool = True) -> None:
        self.frames = frames
        self.close_after = close_after
        super().__init__(("127.0.0.1", 0), _MJPEGHandler)


class _MJPEGHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for image, sequence, timestamp in self.server.frames:
                success, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
                assert success
                jpeg = encoded.tobytes()
                part = (
                    b"--frame\r\nContent-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(jpeg)}\r\n".encode("ascii")
                    + f"X-Sequence: {sequence}\r\nX-Timestamp-Ns: {timestamp}\r\n\r\n".encode("ascii")
                    + jpeg + b"\r\n"
                )
                self.wfile.write(part)
                self.wfile.flush()
            if self.server.close_after:
                self.wfile.write(b"--frame--\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        pass


class NetworkFrameSourceTests(unittest.TestCase):
    def _serve(self, mode: str, image: np.ndarray, sequence: int = 37,
               timestamp: int = 987654321) -> tuple[_MJPEGServer, NetworkFrameSource]:
        server = _MJPEGServer([(image, sequence, timestamp)])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        source = NetworkFrameSource(f"http://127.0.0.1:{server.server_port}", mode)
        source.connect(deadline_s=1)
        def cleanup() -> None:
            source.close()
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.addCleanup(cleanup)
        return server, source

    def test_mouth_frame_transport_and_pixels(self) -> None:
        image = np.tile(np.arange(800, dtype=np.uint8), (400, 1))
        _server, source = self._serve("mouth", image)
        raw_header, payload = source.read_frame()
        fields = TRANSPORT_HEADER.unpack(raw_header)
        self.assertEqual(fields[:9], (b"QPLIVE3\0", 3, 64, 37, 987654321,
                                      800, 400, 800, 1))
        self.assertEqual(fields[9:], (800 * 400, 0x0C, 0))
        self.assertEqual(len(payload), 800 * 400)
        decoded = np.frombuffer(payload, dtype=np.uint8).reshape((400, 800))
        self.assertLess(float(np.mean(np.abs(decoded.astype(np.int16) - image.astype(np.int16)))), 3)

    def test_all_frame_transport_and_pixels(self) -> None:
        image = np.tile((np.arange(2000, dtype=np.uint16) % 256).astype(np.uint8), (400, 1))
        _server, source = self._serve("all", image)
        raw_header, payload = source.read_frame()
        fields = TRANSPORT_HEADER.unpack(raw_header)
        self.assertEqual(fields[3:11], (37, 987654321, 2000, 400, 2000, 1,
                                        2000 * 400, 0x1F))
        decoded = np.frombuffer(payload, dtype=np.uint8).reshape((400, 2000))
        self.assertLess(float(np.mean(np.abs(decoded.astype(np.int16) - image.astype(np.int16)))), 3)

    def test_mismatched_decoded_size_raises_value_error(self) -> None:
        image = np.zeros((400, 799), dtype=np.uint8)
        _server, source = self._serve("mouth", image)
        with self.assertRaises(ValueError):
            source.read_frame()

    def test_server_closing_stream_raises_connection_error(self) -> None:
        image = np.zeros((400, 800), dtype=np.uint8)
        _server, source = self._serve("mouth", image)
        source.read_frame()
        with self.assertRaises(ConnectionError):
            source.read_frame()

    def test_connect_to_closed_port_times_out(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        source = NetworkFrameSource(f"http://127.0.0.1:{port}", "all", timeout_s=0.05)
        with self.assertRaises(ConnectionError):
            source.connect(deadline_s=0.15)

    def test_bare_host_defaults_to_http_and_service_port(self) -> None:
        for url in ("192.168.0.140", "192.168.0.140:27280", "http://192.168.0.140"):
            source = NetworkFrameSource(url, "mouth")
            self.assertEqual((source._scheme, source._host, source._port), ("http", "192.168.0.140", 27280))


if __name__ == "__main__":
    unittest.main()
