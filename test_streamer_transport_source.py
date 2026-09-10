import unittest
from pathlib import Path


class StreamerTransportSourceTests(unittest.TestCase):
    def test_v8_uses_metadata_counter_without_continuous_image_hashing(self):
        source = Path("streamer.c").read_text(encoding="utf-8")
        self.assertIn("FRAME_COUNTER_OFFSET (SENSOR_BYTES + (size_t)24)", source)
        self.assertIn("ring-order=hardware-counter", source)
        self.assertNotIn("static uint64_t fingerprint", source)
        self.assertNotIn("observe_ring_changes", source)

        # At a configured cap, the provider deadline must be checked before
        # any slot selection/image inspection. This guards the v7 regression
        # where all nine DMA surfaces were scanned continuously.
        loop = source.index("for (;;) {")
        deadline = source.index("uint32_t max_fps = requested_max_fps", loop)
        selection = source.index("CameraMap *map = newest_face_slot", deadline)
        self.assertLess(deadline, selection)

    def test_v8_uses_unique_idle_isolation_paths(self):
        streamer = Path("streamer.c").read_text(encoding="utf-8")
        relay = Path("relay.c").read_text(encoding="utf-8")
        launcher = Path("build-and-run.ps1").read_text(encoding="utf-8")
        self.assertIn("questpro-live-v8-shared.bin", streamer)
        self.assertIn("questpro-live-v8-shared.bin", relay)
        self.assertIn("libquestpro-camera-streamer-v8.so", launcher)
        self.assertIn("questpro-camera-relay-v8", launcher)


if __name__ == "__main__":
    unittest.main()
