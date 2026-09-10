import sys
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

import receiver
import tongue_model_preview


class _FakeServer:
    def shutdown(self):
        pass

    def server_close(self):
        pass


class _FakeLabels:
    def __init__(self, *_args, **_kwargs):
        self.sample_count = 0
        self.schema_names = []
        self.path = None

    def close(self):
        pass


class _FakeTonguePreview:
    def __init__(self, checkpoint_path, **_kwargs):
        self.device = "cpu"
        self.checkpoint_path = Path(checkpoint_path)
        self.direction_checkpoint_path = None


class ReceiverTongueStartupTests(unittest.TestCase):
    def test_stream_gap_stats_separates_headset_and_pc_receive_pauses(self):
        stats = receiver.StreamGapStats(
            source_ms=deque(maxlen=240), arrival_ms=deque(maxlen=240)
        )
        stats.add(1_000_000_000, 2_000_000_000)
        stats.add(1_050_000_000, 2_050_000_000)
        stats.add(1_100_000_000, 2_180_000_000)
        source, arrival = stats.summaries()
        self.assertEqual(source, (50.0, 50.0))
        self.assertGreater(arrival[0], 120.0)
        self.assertEqual(arrival[1], 130.0)

    def test_frame_replay_stats_detects_nonconsecutive_exact_payload(self):
        stats = receiver.FrameReplayStats()
        self.assertFalse(stats.add(b"frame-a"))
        self.assertFalse(stats.add(b"frame-b"))
        self.assertFalse(stats.add(b"frame-c"))
        self.assertTrue(stats.add(b"frame-a"))
        self.assertEqual(stats.suspected_replays, 1)
        # A normal duplicate adjacent frame is not classified as ring replay.
        self.assertFalse(stats.add(b"frame-a"))
        self.assertEqual(stats.suspected_replays, 1)

    def test_tongue_preview_does_not_access_unused_pilot_model(self):
        model = Path("models/qpro-stereo-tongue-v1.pt").resolve()
        arguments = [
            "receiver.py",
            "--tongue-model",
            str(model),
            "--tongue-model-device",
            "cpu",
        ]
        with (
            mock.patch.object(sys, "argv", arguments),
            mock.patch.object(receiver, "start_mjpeg_server", return_value=_FakeServer()),
            mock.patch.object(receiver, "LabelSidecarRecorder", _FakeLabels),
            mock.patch.object(tongue_model_preview, "LiveTongueModelPreview", _FakeTonguePreview),
            mock.patch.object(receiver.threading.Thread, "start", return_value=None),
            mock.patch.object(receiver.cv2, "namedWindow"),
            mock.patch.object(receiver.cv2, "resizeWindow"),
            mock.patch.object(receiver.cv2, "setMouseCallback"),
            mock.patch.object(receiver.cv2, "destroyAllWindows"),
            mock.patch.object(receiver.socket, "create_connection", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                receiver.main()


if __name__ == "__main__":
    unittest.main()
