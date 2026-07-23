from __future__ import annotations

from http.server import ThreadingHTTPServer
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from sensor_msgs.msg import Image

from tools.serve_image_raw_snapshot import LatestSnapshot, make_handler


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
        self.snapshot.update(b"jpeg-payload", msg)

    def test_fresh_frame_has_source_metadata(self) -> None:
        self._update()
        with urlopen(f"{self.base_url}/snapshot.jpg", timeout=1.0) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["X-Frame-Sequence"], "1")
            self.assertEqual(response.headers["X-Source-Stamp-Sec"], "123.456")
            self.assertEqual(response.read(), b"jpeg-payload")

    def test_stale_frame_is_rejected(self) -> None:
        self._update()
        time.sleep(0.15)
        with self.assertRaises(HTTPError) as raised:
            urlopen(f"{self.base_url}/snapshot.jpg", timeout=1.0)
        self.assertEqual(raised.exception.code, 503)
        raised.exception.close()


if __name__ == "__main__":
    unittest.main()
