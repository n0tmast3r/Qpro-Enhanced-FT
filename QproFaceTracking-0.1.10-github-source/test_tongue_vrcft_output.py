import struct
import threading
import time
import unittest

import numpy as np

from receiver import SharedPreview, handle_key
from tongue_model_preview import (
    TONGUE_MAGIC,
    TONGUE_PACKET,
    TONGUE_VERSION,
    TonguePrediction,
    TongueInferenceWorker,
    encode_tongue_packet,
    vrcft_tongue_values,
)
from tongue_calibration import TONGUE_TARGET_NAMES


class TongueVrcftOutputTests(unittest.TestCase):
    def prediction(self, *, visible: bool = True) -> TonguePrediction:
        values = np.zeros(len(TONGUE_TARGET_NAMES), dtype=np.float32)
        by_name = {name: index for index, name in enumerate(TONGUE_TARGET_NAMES)}
        values[by_name["extension"]] = 0.7
        values[by_name["horizontal"]] = -0.6
        values[by_name["vertical"]] = 0.4
        values[by_name["curl_up"]] = 0.3
        values[by_name["twist"]] = 0.2
        return TonguePrediction(values, 0.5, 0.8, visible, 1.0)

    def test_signed_model_heads_map_to_separate_vrcft_channels(self):
        values = vrcft_tongue_values(self.prediction(), list(TONGUE_TARGET_NAMES))
        self.assertEqual(values.shape, (12,))
        self.assertAlmostEqual(float(values[0]), 0.8)
        self.assertAlmostEqual(float(values[1]), 0.4)
        self.assertEqual(float(values[2]), 0.0)
        self.assertAlmostEqual(float(values[3]), 0.6)
        self.assertEqual(float(values[4]), 0.0)
        self.assertAlmostEqual(float(values[7]), 0.3)
        self.assertEqual(float(values[10]), 0.0)
        self.assertAlmostEqual(float(values[11]), 0.2)

    def test_retracted_gate_zeros_all_experimental_channels(self):
        values = vrcft_tongue_values(
            self.prediction(visible=False), list(TONGUE_TARGET_NAMES)
        )
        self.assertFalse(np.any(values))

    def test_packet_contract_and_disabled_flag(self):
        packet = encode_tongue_packet(np.linspace(0, 1, 12), enabled=False)
        self.assertEqual(len(packet), TONGUE_PACKET.size)
        magic, version, flags, reserved, *values = struct.unpack(
            TONGUE_PACKET.format, packet
        )
        self.assertEqual(magic, TONGUE_MAGIC)
        self.assertEqual(version, TONGUE_VERSION)
        self.assertEqual(flags, 0)
        self.assertEqual(reserved, 0)
        self.assertAlmostEqual(values[-1], 1.0)

    def test_t_key_is_a_runtime_toggle_not_a_calibration_command(self):
        shared = SharedPreview()
        handle_key(shared, "t")
        self.assertEqual(shared.take_runtime_keys(), ["t"])
        self.assertEqual(shared.take_calibration_keys(), [])

    def test_inference_worker_replaces_pending_stale_frame(self):
        started = threading.Event()
        release = threading.Event()

        class Preview:
            target_names = list(TONGUE_TARGET_NAMES)

            def predict(self, strip, _sample, _names):
                value = float(strip[0, 0])
                if value == 0.0:
                    started.set()
                    release.wait(timeout=1.0)
                values = np.zeros(len(self.target_names), dtype=np.float32)
                values[0] = value
                return TonguePrediction(values, 0.0, 0.0, False, 1.0)

            def render(self, prediction, *, output_enabled=False):
                del output_enabled
                return np.asarray([[prediction.values[0]]], dtype=np.float32)

        class Broadcaster:
            enabled = False

            def send_prediction(self, _prediction, _names):
                pass

        worker = TongueInferenceWorker(Preview(), Broadcaster())
        try:
            worker.submit(np.zeros((1, 1), dtype=np.uint8), None, [])
            self.assertTrue(started.wait(timeout=1.0))
            worker.submit(np.ones((1, 1), dtype=np.uint8), None, [])
            worker.submit(np.full((1, 1), 2, dtype=np.uint8), None, [])
            release.set()
            deadline = time.monotonic() + 1.0
            prediction = None
            while time.monotonic() < deadline:
                prediction, _image = worker.latest()
                if prediction is not None and prediction.values[0] == 2.0:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(prediction)
            self.assertEqual(float(prediction.values[0]), 2.0)
            self.assertGreaterEqual(prediction.dropped_frames, 1)
        finally:
            release.set()
            worker.close()


if __name__ == "__main__":
    unittest.main()
