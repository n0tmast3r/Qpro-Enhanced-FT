import math
import struct
import unittest
from pathlib import Path

from native_eye_probe import NativeEyeSample, parse_memory_client1
from native_eye_pupil_probe import KernelToPcMonotonicClock
from native_raw_eye_probe import (
    DetectorOutputParser,
    PreMergeParser,
    RawEyeSample,
    TracePairParser,
    float_from_trace_hex,
)


class NativeEyeProbeTests(unittest.TestCase):
    def test_trace_cleanup_uses_eof_instead_of_remote_pkill(self) -> None:
        source = Path("native_raw_eye_probe.py").read_text(encoding="utf-8")
        self.assertIn("free_buffer", source)
        self.assertNotIn("pkill -f '^cat", source)

    def test_trace_control_uses_one_persistent_magisk_shell(self) -> None:
        source = Path("native_raw_eye_probe.py").read_text(encoding="utf-8")
        self.assertIn("class PersistentAdbRootShell", source)
        self.assertIn("__QPRO_ROOT_DONE_", source)
        self.assertNotIn('[self.adb, "shell", "su", "-c", command]', source)

    def test_detector_reports_true_ray_separation(self) -> None:
        angle = math.radians(10.0)
        sample = RawEyeSample(
            pc_monotonic_ns=1,
            kernel_time_s=1.0,
            left_valid=True,
            right_valid=True,
            left_vector=(0.0, 0.0, 1.0),
            right_vector=(math.sin(angle), 0.0, math.cos(angle)),
        )
        self.assertAlmostEqual(sample.disparity, 10.0, places=5)
        self.assertAlmostEqual(sample.pitch_difference, 0.0, places=5)
        self.assertAlmostEqual(sample.angular_separation, 10.0, places=5)

    def test_kernel_clock_translation_removes_variable_usb_batch_delay(self) -> None:
        clock = KernelToPcMonotonicClock(window=16)
        clock_offset = 50_000_000_000
        delays = [80_000_000, 45_000_000, 0, 70_000_000]
        translated = []
        for index, delay in enumerate(delays):
            kernel_ns = 10_000_000_000 + index * 13_000_000
            arrival = kernel_ns + clock_offset + delay
            translated.append(clock.translate(kernel_ns / 1_000_000_000, arrival))
        self.assertEqual(clock.estimated_offset_ns, clock_offset)
        self.assertEqual(translated[-1], 10_039_000_000 + clock_offset)

    def test_independent_pose_disparity_is_computed_before_combined_point(self) -> None:
        sample = NativeEyeSample(
            pc_monotonic_ns=1,
            mode=0,
            valid=True,
            source_time=1.0,
            arrival_time=1.0,
            processing_end_time=1.0,
            left_position=(-0.032, 0.0, 0.0),
            left_orientation=(0.0, 0.0, 0.0, 1.0),
            right_position=(0.032, 0.0, 0.0),
            right_orientation=(0.0, 0.0871557, 0.0, 0.9961947),
            combined_point=(0.0, 0.0, -1.1),
        )
        self.assertAlmostEqual(sample.left_angles[0], 0.0, places=4)
        self.assertAlmostEqual(sample.right_angles[0], -10.0, places=3)
        self.assertAlmostEqual(sample.disparity, -10.0, places=3)

    def test_memory_client_parser_decodes_both_eye_records(self) -> None:
        payload = bytearray(256)
        payload[0] = 1
        struct.pack_into("<Q", payload, 8, 1_000_000_000)
        struct.pack_into("<Q", payload, 16, 2_000_000_000)
        struct.pack_into("<Q", payload, 24, 3_000_000_000)
        struct.pack_into("<4f", payload, 40, 0.0, 0.0, 0.0, 1.0)
        struct.pack_into("<3f", payload, 56, -0.032, 0.0, 0.0)
        struct.pack_into("<4f", payload, 104, 0.0, 0.1, 0.0, 0.995)
        struct.pack_into("<3f", payload, 120, 0.032, 0.0, 0.0)
        struct.pack_into("<3f", payload, 200, 0.2, -0.1, -1.1)
        sample = parse_memory_client1(
            "kind=client1 mode=3 status=1 bytes=" + payload.hex(), 99
        )
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.mode, 3)
        self.assertEqual(sample.pc_monotonic_ns, 99)
        self.assertAlmostEqual(sample.source_time, 1.0)
        self.assertAlmostEqual(sample.left_position[0], -0.032, places=5)
        self.assertAlmostEqual(sample.right_orientation[1], 0.1, places=5)
        self.assertAlmostEqual(sample.combined_point[2], -1.1, places=5)

    def test_raw_trace_parser_pairs_eyes_and_deduplicates_publishers(self) -> None:
        parser = TracePairParser()
        left = (
            "FaceCam-10 [001] .... 12.345000: qpro_left: (0x1) "
            "x=0x3f000000 y=0x00000000 z=0x3f800000 valid=1"
        )
        right = (
            "FaceCam-10 [001] .... 12.345010: qpro_right: (0x2) "
            "x=0xbf000000 y=0x00000000 z=0x3f800000 valid=1"
        )
        self.assertIsNone(parser.parse(left, 100))
        sample = parser.parse(right, 101)
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertAlmostEqual(sample.left_vector[0], 0.5)
        self.assertAlmostEqual(sample.right_vector[0], -0.5)
        self.assertTrue(sample.valid)
        self.assertLess(sample.disparity, 0.0)
        self.assertIsNone(parser.parse(left, 102))
        self.assertIsNone(parser.parse(right, 103))

    def test_trace_hex_conversion_preserves_signed_float_bits(self) -> None:
        self.assertAlmostEqual(float_from_trace_hex("bec89662"), -0.3917723, places=5)

    def test_premerge_parser_maps_x0_right_and_x1_left(self) -> None:
        parser = PreMergeParser()
        line = (
            "FaceCam-10 [001] .... 12.500000: qpro_inputs: (0x1) "
            "ax=0x3f000000 ay=0x00000000 az=0x3f800000 av=1 "
            "bx=0xbf000000 by=0x00000000 bz=0x3f800000 bv=1"
        )
        sample = parser.parse(line, 99)
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertAlmostEqual(sample.right_vector[0], 0.5)
        self.assertAlmostEqual(sample.left_vector[0], -0.5)
        self.assertTrue(sample.valid)
        self.assertIsNone(parser.parse(line, 100))

    def test_detector_parser_pairs_eye_tags_zero_and_one(self) -> None:
        parser = DetectorOutputParser()
        left = (
            "FaceCam-10 [001] .... 12.500000: detector_output: (0x1) "
            "x=0x3f000000 y=0x00000000 z=0x3f800000 tag=0x61630000"
        )
        right = (
            "FaceCam-10 [001] .... 12.500100: detector_output: (0x1) "
            "x=0xbf000000 y=0x00000000 z=0x3f800000 tag=0x61630001"
        )
        self.assertIsNone(parser.parse(left, 100))
        sample = parser.parse(right, 101)
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertAlmostEqual(sample.left_vector[0], 0.5)
        self.assertAlmostEqual(sample.right_vector[0], -0.5)
        self.assertTrue(sample.valid)


if __name__ == "__main__":
    unittest.main()
