import tempfile
import time
import unittest
from pathlib import Path

from capture_format import CaptureWriter, TRANSPORT_HEADER, inspect_capture


class CaptureFormatTests(unittest.TestCase):
    def test_round_trip_preserves_face_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "face.qpcap"
            writer = CaptureWriter(path)
            payload = bytes(1200 * 400)
            for sequence in (10, 12):
                source_header = TRANSPORT_HEADER.pack(
                    b"QPLIVE3\0", 3, TRANSPORT_HEADER.size, sequence,
                    123_000_000 + sequence, 1200, 400, 1200, 1,
                    len(payload), 0b11100, 7,
                )
                writer.write(
                    source_header, payload,
                    1_000_000_000 + sequence * 1_000_000,
                    time.time_ns(),
                )
            writer.close()

            summary = inspect_capture(path)
            self.assertTrue(summary["completed"])
            self.assertFalse(summary["truncated"])
            self.assertEqual(summary["declared_frames"], 2)
            self.assertEqual(summary["scanned_frames"], 2)
            self.assertEqual(summary["camera_masks"], {0b11100: 2})
            self.assertEqual(summary["dimensions"], {(1200, 400): 2})
            self.assertEqual(summary["first_sequence"], 10)
            self.assertEqual(summary["last_sequence"], 12)
            self.assertEqual(summary["source_skips"], 1)
            self.assertEqual(summary["exact_nonconsecutive_replays"], 0)
            self.assertEqual(summary["rejected_torn_delta"], 0)
            self.assertGreater(summary["duration_seconds"], 0)

    def test_incomplete_capture_remains_scannable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interrupted.qpcap"
            writer = CaptureWriter(path)
            payload = bytes(400 * 400)
            source_header = TRANSPORT_HEADER.pack(
                b"QPLIVE3\0", 3, TRANSPORT_HEADER.size, 1, 100, 400, 400,
                400, 1, len(payload), 0b00001, 0,
            )
            writer.write(source_header, payload, time.monotonic_ns(), time.time_ns())
            writer.close(completed=False)

            summary = inspect_capture(path)
            self.assertFalse(summary["completed"])
            self.assertFalse(summary["truncated"])
            self.assertEqual(summary["scanned_frames"], 1)

    def test_inspector_counts_only_nonconsecutive_exact_replays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replay.qpcap"
            writer = CaptureWriter(path)
            payloads = [bytes([1]) * 100, bytes([2]) * 100,
                        bytes([1]) * 100, bytes([1]) * 100]
            for sequence, payload in enumerate(payloads, start=1):
                source_header = TRANSPORT_HEADER.pack(
                    b"QPLIVE3\0", 3, TRANSPORT_HEADER.size, sequence,
                    100 + sequence, 100, 1, 100, 1, len(payload), 1, 0,
                )
                writer.write(
                    source_header, payload, time.monotonic_ns(), time.time_ns()
                )
            writer.close()
            summary = inspect_capture(path)
            self.assertEqual(summary["exact_nonconsecutive_replays"], 1)


if __name__ == "__main__":
    unittest.main()
