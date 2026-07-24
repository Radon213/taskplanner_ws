from __future__ import annotations

from http.server import ThreadingHTTPServer
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from sensor_msgs.msg import Image

from tools.image_raw_to_compressed import ImageRawToCompressed
from tools.serve_image_raw_snapshot import LatestSnapshot, decode_image, make_handler
from vlm_node.snapshot_bridge import SnapshotSequenceGate


class SnapshotFreshnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = LatestSnapshot()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.snapshot, max_frame_age_sec=0.1),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1.0)

    def _update(self) -> None:
        msg = Image()
        msg.header.stamp.sec = 123
        msg.header.stamp.nanosec = 456_000_000
        msg.header.frame_id = "camera"
        msg.width = 1
        msg.height = 1
        msg.encoding = "rgb8"
        msg.step = 3
        msg.data = bytes([0, 0, 0])
        self.snapshot.update(b"jpeg-payload", msg)

    def test_fresh_frame_has_source_metadata(self) -> None:
        self._update()
        with urlopen(f"{self.base_url}/snapshot.jpg", timeout=1.0) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["X-Frame-Sequence"], "1")
            self.assertEqual(response.headers["X-Source-Stamp-Sec"], "123.456")
            self.assertTrue(response.headers["X-Source-Instance"])
            self.assertEqual(response.read(), b"jpeg-payload")

    def test_stale_frame_is_rejected(self) -> None:
        self._update()
        time.sleep(0.15)
        with self.assertRaises(HTTPError) as raised:
            urlopen(f"{self.base_url}/snapshot.jpg", timeout=1.0)
        self.assertEqual(raised.exception.code, 503)
        raised.exception.close()

    def test_padded_rgb_rows_decode_without_shift(self) -> None:
        msg = Image()
        msg.width = 2
        msg.height = 2
        msg.encoding = "rgb8"
        msg.step = 8
        msg.data = bytes(
            [
                255, 0, 0, 0, 255, 0, 99, 99,
                0, 0, 255, 255, 255, 255, 88, 88,
            ]
        )
        image = decode_image(msg)
        converted = ImageRawToCompressed._decode_image(msg)
        for decoded in (image, converted):
            self.assertEqual(decoded.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(decoded.getpixel((1, 0)), (0, 255, 0))
            self.assertEqual(decoded.getpixel((0, 1)), (0, 0, 255))
            self.assertEqual(decoded.getpixel((1, 1)), (255, 255, 255))


class SnapshotSequenceGateTest(unittest.TestCase):
    def test_new_source_instance_resets_sequence(self) -> None:
        gate = SnapshotSequenceGate()
        self.assertTrue(gate.accept("server-a", 10))
        self.assertFalse(gate.accept("server-a", 10))
        self.assertTrue(gate.accept("server-b", 1))
        self.assertTrue(gate.accept("", 1))

    def test_legacy_sequence_regression_recovers(self) -> None:
        gate = SnapshotSequenceGate()
        self.assertTrue(gate.accept("", 10))
        self.assertTrue(gate.accept("", 1))


if __name__ == "__main__":
    unittest.main()
