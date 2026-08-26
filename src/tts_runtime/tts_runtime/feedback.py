"""Pure, fail-closed conversion of controller feedback into fixed TTS replies.

The converter observes public Taskplanner facts only.  It neither admits nor
executes a robot command.  Durable deduplication remains the responsibility of
the :class:`tts_runtime.core.PlaybackStore`; the in-memory command set below
only suppresses repeated controller progress updates within one process.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import threading

from .core import ReplyRequest


THYROIDECTOMY_DEMO = "thyroidectomy_demo"
RETRIEVAL_ACTIONS = frozenset({"retrieve_from_mayo", "tool_retrieve"})
RETRIEVAL_PROGRESS_STATES = frozenset(
    {
        "moving_to_source",
        "grasping",
        "moving_to_target",
        "placing",
        "holding",
    }
)
RETRIEVAL_STARTED_SEMANTIC_KEY = "tool_retrieval_started"
RETRIEVAL_STARTED_TEXT = "도구 회수중입니다"


@dataclass(frozen=True)
class GatewayScope:
    """Normalized public gateway scope used to qualify feedback.

    ``started_stamp_ns`` is the timestamp of the first ``GatewayInfo`` seen
    after a scope transition.  When it is positive, a status must also carry a
    positive, strictly newer timestamp.  This rejects retained feedback from a
    previous run instead of relabelling it with the current run ID.
    """

    gateway_instance_id: str
    procedure_run_id: str
    procedure_type: str
    procedure_active: bool
    started_stamp_ns: int = 0

    @property
    def key(self) -> tuple[str, str, str, bool]:
        return (
            self.gateway_instance_id,
            self.procedure_run_id,
            self.procedure_type,
            self.procedure_active,
        )

    @property
    def permits_retrieval_feedback(self) -> bool:
        return bool(
            self.procedure_active
            and self.gateway_instance_id
            and self.procedure_run_id
            and self.procedure_type == THYROIDECTOMY_DEMO
        )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def retrieval_feedback_digest(
    *,
    gateway_instance_id: str,
    procedure_run_id: str,
    command_id: str,
) -> str:
    """Return the stable SHA-256 identity for one semantic announcement."""

    material = {
        "command_id": command_id,
        "gateway_instance_id": gateway_instance_id,
        "procedure_run_id": procedure_run_id,
        "schema": "taskplanner.tts.fixed-feedback.v1",
        "semantic_key": RETRIEVAL_STARTED_SEMANTIC_KEY,
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


class RetrievalFeedbackAnnouncer:
    """Emit one immediate retrieval-progress reply per command and run scope."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._scope: GatewayScope | None = None
        self._announced_command_ids: set[str] = set()

    @property
    def scope(self) -> GatewayScope | None:
        with self._lock:
            return self._scope

    def observe_gateway_info(
        self,
        *,
        gateway_instance_id: str,
        procedure_run_id: str,
        procedure_type: str,
        procedure_active: bool,
        stamp_ns: int = 0,
    ) -> None:
        """Set the current authoritative scope and reset noise dedupe on change.

        Repeated heartbeats for the same scope do not move the start boundary;
        otherwise a valid status could become older than a later heartbeat.
        """

        candidate = GatewayScope(
            gateway_instance_id=gateway_instance_id.strip(),
            procedure_run_id=procedure_run_id.strip(),
            procedure_type=procedure_type.strip(),
            procedure_active=bool(procedure_active),
            started_stamp_ns=max(0, int(stamp_ns)),
        )
        with self._lock:
            if self._scope is not None and candidate.key == self._scope.key:
                return
            self._scope = candidate
            self._announced_command_ids.clear()

    def observe_skill_status(
        self,
        *,
        command_id: str,
        action: str,
        state: str,
        success: bool,
        message: str,
        stamp_ns: int = 0,
    ) -> ReplyRequest | None:
        """Convert the first exact controller progress fact, or fail closed."""

        normalized_command_id = command_id.strip()
        normalized_action = action.strip()
        normalized_state = state.strip()
        status_stamp_ns = max(0, int(stamp_ns))

        with self._lock:
            scope = self._scope
            if scope is None or not scope.permits_retrieval_feedback:
                return None
            if not normalized_command_id:
                return None
            if normalized_action not in RETRIEVAL_ACTIONS:
                return None
            if normalized_state not in RETRIEVAL_PROGRESS_STATES:
                return None
            if success is not True or message != "executing":
                return None
            if scope.started_stamp_ns > 0 and (
                status_stamp_ns <= scope.started_stamp_ns
            ):
                return None
            if normalized_command_id in self._announced_command_ids:
                return None

            digest = retrieval_feedback_digest(
                gateway_instance_id=scope.gateway_instance_id,
                procedure_run_id=scope.procedure_run_id,
                command_id=normalized_command_id,
            )
            self._announced_command_ids.add(normalized_command_id)
            return ReplyRequest(
                reply_id=f"fixed-feedback:{digest}",
                turn_id=f"fixed-turn:{digest}",
                utterance_id=f"fixed-utterance:{digest}",
                gateway_instance_id=scope.gateway_instance_id,
                procedure_run_id=scope.procedure_run_id,
                text=RETRIEVAL_STARTED_TEXT,
                timing="immediate",
            )
