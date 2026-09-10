import json
import socket
import tempfile
import time
import unittest
from pathlib import Path

from label_capture import LabelSidecarRecorder, inspect_sidecar


class LabelCaptureTests(unittest.TestCase):
    def test_records_schema_and_timestamped_sample(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labels.qplabel.jsonl"
            recorder = LabelSidecarRecorder(path, port=port)
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.sendto(
                json.dumps(
                    {"v": 1, "type": "schema", "names": ["JawOpen", "BrowInnerUpLeft"]}
                ).encode(),
                ("127.0.0.1", port),
            )
            sender.sendto(
                json.dumps(
                    {
                        "v": 1,
                        "type": "sample",
                        "sequence": 1,
                        "qpc": 123456,
                        "qpcFrequency": 10_000_000,
                        "utcUnixMs": 1_700_000_000_000,
                        "sourceChangeSequence": 4,
                        "sourceUnchangedMs": 25.0,
                        "values": [0.25, 0.75],
                    }
                ).encode(),
                ("127.0.0.1", port),
            )
            deadline = time.monotonic() + 1.0
            while recorder.sample_count < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(recorder.source_is_live())
            nearest = recorder.nearest_sample(time.monotonic_ns())
            self.assertIsNotNone(nearest)
            self.assertEqual(nearest["values"], [0.25, 0.75])
            recorder.close()
            sender.close()

            summary = inspect_sidecar(path)
            self.assertTrue(summary["completed"])
            self.assertEqual(summary["schemas"], 1)
            self.assertEqual(summary["samples"], 1)
            self.assertEqual(summary["expressions"], 2)
            self.assertEqual(summary["invalid_lines"], 0)


if __name__ == "__main__":
    unittest.main()
