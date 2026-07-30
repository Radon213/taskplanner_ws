from tools.real_surgery_annotation.rosbag_compat import (
    close_reader,
    read_next_record,
)


class _JazzyReader:
    def read_next(self):
        return "/topic", b"payload", 123


class _ExtendedReader:
    closed = False

    def read_next_ext(self):
        return "/topic", b"payload", 456, {"metadata": True}

    def close(self):
        self.closed = True


def test_read_next_record_supports_jazzy_api():
    assert read_next_record(_JazzyReader()) == ("/topic", b"payload", 123)


def test_read_next_record_prefers_extended_api_and_ignores_metadata():
    assert read_next_record(_ExtendedReader()) == ("/topic", b"payload", 456)


def test_close_reader_is_optional():
    close_reader(_JazzyReader())
    reader = _ExtendedReader()
    close_reader(reader)
    assert reader.closed
