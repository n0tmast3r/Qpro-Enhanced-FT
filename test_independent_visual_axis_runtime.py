import math
import socket
import struct
import time
import unittest

import numpy as np

from independent_visual_axis_runtime import (
    PACKET_FORMAT,
    PACKET_MAGIC,
    PACKET_SIZE,
    PACKET_VERSION,
    VergenceControl,
    amplify_vergence,
    calibrated_angles,
    encode_packet,
    vrcft_angles,
)
from native_raw_eye_probe import RawEyeSample


class RuntimeContractTests(unittest.TestCase):
    def test_vergence_gain_preserves_mean_gaze_and_scales_eye_separation(self):
        left = np.asarray([10.0, -5.0])
        right = np.asarray([-20.0, 7.0])
        for gain in (0.0, 1.0, 2.0, 4.0):
            with self.subTest(gain=gain):
                scaled_left, scaled_right = amplify_vergence(left, right, gain)
                np.testing.assert_allclose((scaled_left + scaled_right) / 2.0, [-5.0, 1.0])
                np.testing.assert_allclose(scaled_right - scaled_left, [-30.0 * gain, 12.0 * gain])
        np.testing.assert_array_equal(left, [10.0, -5.0])
        np.testing.assert_array_equal(right, [-20.0, 7.0])

    def test_vergence_control_updates_gain_over_localhost_udp(self):
        control = VergenceControl(1.0, port=0)
        self.addCleanup(control.close)
        self.assertIsNotNone(control._socket)
        assert control._socket is not None
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"2.5", control._socket.getsockname())
        deadline = time.monotonic() + 1.0
        while control.gain != 2.5 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(control.gain, 2.5)

    def test_v2_calibration_maps_detector_tags_to_physical_eyes(self):
        identity = {"coefficients": [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]]}
        calibration = {
            "left": identity,
            "right": identity,
            "detector_tag_mapping": {
                "physical_left": "trace_tag_1",
                "physical_right": "trace_tag_0",
            },
        }
        sample = RawEyeSample(
            pc_monotonic_ns=1,
            kernel_time_s=1.0,
            left_valid=True,
            right_valid=True,
            left_vector=(0.1, -0.2, 1.0),
            right_vector=(-0.3, 0.4, 1.0),
        )
        left, right = calibrated_angles(calibration, sample)
        self.assertTrue(np.allclose(left, sample.right_angles))
        self.assertTrue(np.allclose(right, sample.left_angles))

    def test_vrcft_conversion_does_not_mirror_calibrated_yaw(self):
        x, y = vrcft_angles(np.asarray([-30.0, 15.0]))
        self.assertTrue(math.isclose(x, math.radians(-30.0)))
        self.assertTrue(math.isclose(y, math.radians(15.0)))

    def test_packet_contract_and_validity_bits(self):
        packet = encode_packet(np.asarray([10.0, -5.0]), np.asarray([-20.0, 7.0]))
        self.assertEqual(len(packet), PACKET_SIZE)
        magic, version, flags, reserved, lx, ly, rx, ry = struct.unpack(
            PACKET_FORMAT, packet
        )
        self.assertEqual(magic, PACKET_MAGIC)
        self.assertEqual(version, PACKET_VERSION)
        self.assertEqual(flags, 3)
        self.assertEqual(reserved, 0)
        # The packet fields are VRCFT channel names, whose physical ownership
        # is reversed by the final VRChat boundary test.
        self.assertTrue(math.isclose(lx, math.radians(-20.0), abs_tol=1e-6))
        self.assertTrue(math.isclose(ly, math.radians(7.0), abs_tol=1e-6))
        self.assertTrue(math.isclose(rx, math.radians(10.0), abs_tol=1e-6))
        self.assertTrue(math.isclose(ry, math.radians(-5.0), abs_tol=1e-6))

    def test_packet_validity_bits_follow_crossed_vrcft_channels(self):
        packet = encode_packet(
            np.asarray([1.0, 2.0]), np.asarray([3.0, 4.0]),
            left_valid=True, right_valid=False,
        )
        _magic, _version, flags, *_values = struct.unpack(PACKET_FORMAT, packet)
        self.assertEqual(flags, 2)


if __name__ == "__main__":
    unittest.main()
