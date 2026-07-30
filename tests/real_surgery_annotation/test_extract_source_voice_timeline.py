from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jsonschema

from tools.real_surgery_annotation.extract_source_voice_timeline import (
    AVAILABILITY_POLICY,
    SCORING_ROLE,
    SOURCE_AUTHORITY,
    TranscriptRecord,
    VOICE_SCHEMA,
    VoiceTimelineError,
    build_voice_events,
    extract_voice_timeline,
    serialize_jsonl,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = (
    ROOT
    / "annotations/observable_tool_events/schema/"
    "observable_voice_point.v2.schema.json"
)


def source_record(
    index: int,
    *,
    start_sec: float | None = None,
    end_sec: float | None = None,
    available_sec: float | None = None,
    record_timestamp_sec: float | None = None,
    text: str | None = None,
) -> TranscriptRecord:
    exact_start_sec = index + 0.125 if start_sec is None else start_sec
    exact_end_sec = (
        exact_start_sec + 0.5
        if end_sec is None
        else end_sec
    )
    payload = {
        "source_wav": f"source-{index:02d}.wav",
        "dji_start_sec": exact_start_sec + 4.5,
        "dji_end_sec": exact_start_sec + 5.0,
        "start_sec": exact_start_sec,
        "end_sec": exact_end_sec,
        "text": f"원문 발화 {index}" if text is None else text,
    }
    if available_sec is not None:
        payload["available_sec"] = available_sec
    return TranscriptRecord(
        timestamp_ns=round(
            (
                exact_start_sec
                if record_timestamp_sec is None
                else record_timestamp_sec
            )
            * 1_000_000_000
        ),
        raw_text=json.dumps(payload, ensure_ascii=False),
    )


class SourceVoiceTimelineTest(unittest.TestCase):
    def test_schema_accepts_context_point_and_forbids_frame_index(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        event = build_voice_events(
            case_id="0704_6",
            records=[source_record(1)],
            expected_count=1,
        )[0]
        validator = jsonschema.Draft202012Validator(schema)
        self.assertEqual([], list(validator.iter_errors(event)))

        with_frame = {**event, "source_frame_idx": 160}
        errors = list(validator.iter_errors(with_frame))
        self.assertTrue(errors)
        self.assertIn("source_frame_idx", errors[0].message)

    def test_all_22_messages_keep_source_order_exact_text_and_time(self) -> None:
        records = [
            source_record(index, text=f"  원문 {index} 그대로  ")
            for index in range(1, 23)
        ]
        events = build_voice_events(
            case_id="0704_6",
            records=records,
            expected_count=22,
        )

        self.assertEqual(22, len(events))
        self.assertEqual(
            [f"0704_6-V{index:04d}" for index in range(1, 23)],
            [event["event_id"] for event in events],
        )
        for index, (record, event) in enumerate(zip(records, events), 1):
            payload = json.loads(record.raw_text)
            self.assertEqual(payload["start_sec"], event["time_sec"])
            self.assertEqual(payload["end_sec"], event["end_sec"])
            self.assertEqual(payload["end_sec"], event["available_sec"])
            self.assertEqual(payload["text"], event["text"])
            self.assertEqual(index, event["source_message_index"])
            self.assertEqual(
                record.timestamp_ns,
                event["source_record_timestamp_ns"],
            )
            self.assertEqual(SOURCE_AUTHORITY, event["source_authority"])
            self.assertEqual(SCORING_ROLE, event["scoring_role"])
            self.assertEqual(VOICE_SCHEMA, event["schema"])
            self.assertEqual(
                AVAILABILITY_POLICY,
                event["availability_policy"],
            )
            self.assertNotIn("source_frame_idx", event)

    def test_availability_is_never_before_end_or_late_record_arrival(self) -> None:
        late_record = source_record(
            1,
            start_sec=1.0,
            end_sec=1.5,
            record_timestamp_sec=1.75,
        )
        event = build_voice_events(
            case_id="0704_6",
            records=[late_record],
            expected_count=1,
        )[0]
        self.assertEqual(1.0, event["time_sec"])
        self.assertEqual(1.5, event["end_sec"])
        self.assertEqual(1.75, event["available_sec"])

        explicit_later = source_record(
            2,
            start_sec=2.0,
            end_sec=2.5,
            available_sec=2.75,
        )
        event = build_voice_events(
            case_id="0704_6",
            records=[explicit_later],
            expected_count=1,
        )[0]
        self.assertEqual(2.75, event["available_sec"])

    def test_rejects_record_or_availability_before_source_timing(self) -> None:
        before_start = source_record(
            1,
            start_sec=1.0,
            record_timestamp_sec=0.999,
        )
        with self.assertRaisesRegex(
            VoiceTimelineError,
            "MCAP record timestamp .* precedes start_sec",
        ):
            build_voice_events(
                case_id="0704_6",
                records=[before_start],
                expected_count=1,
            )

        before_end = source_record(
            2,
            start_sec=2.0,
            end_sec=2.5,
            available_sec=2.4,
        )
        with self.assertRaisesRegex(
            VoiceTimelineError,
            "available_sec precedes end_sec",
        ):
            build_voice_events(
                case_id="0704_6",
                records=[before_end],
                expected_count=1,
            )

    def test_rejects_wrong_count_and_out_of_order_records(self) -> None:
        with self.assertRaisesRegex(
            VoiceTimelineError,
            "expected 22 transcript messages, found 1",
        ):
            build_voice_events(
                case_id="0704_6",
                records=[source_record(1)],
                expected_count=22,
            )

        with self.assertRaisesRegex(
            VoiceTimelineError,
            "strictly increasing",
        ):
            build_voice_events(
                case_id="0704_6",
                records=[source_record(2), source_record(1)],
                expected_count=2,
            )

    def test_jsonl_is_deterministic_and_preserves_unicode(self) -> None:
        events = build_voice_events(
            case_id="0704_6",
            records=[source_record(1, text="Adson 하나 더")],
            expected_count=1,
        )
        first = serialize_jsonl(events)
        second = serialize_jsonl(events)

        self.assertEqual(first, second)
        self.assertIn("Adson 하나 더", first)
        self.assertEqual(events, [json.loads(first)])

    def test_publication_is_create_only_and_does_not_replace_existing(self) -> None:
        records = [source_record(index) for index in range(1, 23)]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "voice.jsonl"
            with mock.patch(
                "tools.real_surgery_annotation.extract_source_voice_timeline."
                "read_transcript_records",
                return_value=records,
            ):
                events = extract_voice_timeline(
                    source_bag=Path(temporary) / "source",
                    output=output,
                    case_id="0704_6",
                )
                published = output.read_text(encoding="utf-8")
                self.assertEqual(
                    events,
                    [
                        json.loads(line)
                        for line in published.splitlines()
                    ],
                )

                with self.assertRaisesRegex(
                    VoiceTimelineError,
                    "refusing to overwrite existing output",
                ):
                    extract_voice_timeline(
                        source_bag=Path(temporary) / "source",
                        output=output,
                        case_id="0704_6",
                    )
            self.assertEqual(published, output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
