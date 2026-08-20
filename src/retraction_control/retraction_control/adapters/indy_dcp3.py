"""Fail-closed production shell for a Neuromeka IndyDCP3 backend.

The vendor package is intentionally not imported here.  Deployment code must
construct a version-pinned backend and inject it explicitly; construction of
this shell has no network or robot side effects.
"""

from __future__ import annotations

from typing import Any, Sequence

from . import (
    AdapterError,
    AdapterUnavailableError,
    ControllerState,
    JointStateSample,
)


class IndyDcp3Adapter:
    """Validate and translate an explicitly supplied vendor backend.

    The backend must provide the same narrowly named operations as this class.
    A later, version-specific integration module may translate those calls to
    Neuromeka 3.5.0.7 without leaking that dependency into the control core.
    """

    component = "indy_dcp3"

    def __init__(self, backend: object | None = None) -> None:
        self._backend = backend
        self._closed = False

    @property
    def configured(self) -> bool:
        return self._backend is not None and not self._closed

    def _method(self, name: str):
        if self._closed or self._backend is None:
            raise AdapterUnavailableError(
                "indy_backend_unconfigured",
                "an explicit, version-pinned IndyDCP3 backend is required",
                component=self.component,
                operation=name,
            )
        method = getattr(self._backend, name, None)
        if not callable(method):
            raise AdapterUnavailableError(
                "indy_backend_incompatible",
                f"configured IndyDCP3 backend does not provide {name}()",
                component=self.component,
                operation=name,
            )
        return method

    def _invoke(self, name: str, *args: Any, **kwargs: Any) -> Any:
        method = self._method(name)
        try:
            result = method(*args, **kwargs)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(
                "indy_sdk_error",
                f"IndyDCP3 {name} failed: {exc}",
                component=self.component,
                operation=name,
            ) from exc
        if result is False:
            raise AdapterError(
                "indy_sdk_rejected",
                f"IndyDCP3 {name} returned a rejection",
                component=self.component,
                operation=name,
            )
        return result

    def set_friction_compensation(self, value: Any) -> None:
        self._invoke("set_friction_compensation", value)

    def set_custom_gain(self, enabled: bool, gain: Any | None = None) -> None:
        self._invoke("set_custom_gain", bool(enabled), gain)

    def set_direct_teaching(self, enabled: bool) -> None:
        self._invoke("set_direct_teaching", bool(enabled))

    def is_direct_teaching_enabled(self) -> bool:
        return bool(self._invoke("is_direct_teaching_enabled"))

    def read_joint_state(self, arm_id: str) -> JointStateSample:
        value = self._invoke("read_joint_state", arm_id)
        if isinstance(value, JointStateSample):
            return value
        raise AdapterError(
            "invalid_joint_state",
            "IndyDCP3 backend returned an untyped joint state",
            component=self.component,
            operation="read_joint_state",
        )

    def move_joint_positions(
        self, arm_id: str, positions: Sequence[float], *, waypoint_name: str = ""
    ) -> None:
        self._invoke(
            "move_joint_positions",
            arm_id,
            tuple(float(value) for value in positions),
            waypoint_name=waypoint_name,
        )

    def jog_tcp(
        self,
        arm_id: str,
        *,
        axis: str,
        distance_mm: float,
        frame: str,
    ) -> None:
        self._invoke(
            "jog_tcp",
            arm_id,
            axis=str(axis),
            distance_mm=float(distance_mm),
            frame=str(frame),
        )

    def stop_motion(self) -> None:
        self._invoke("stop_motion")

    def hold_position(self) -> None:
        self._invoke("hold_position")

    def controller_state(self) -> ControllerState:
        value = self._invoke("controller_state")
        if isinstance(value, ControllerState):
            return value
        raise AdapterError(
            "invalid_controller_state",
            "IndyDCP3 backend returned an untyped controller state",
            component=self.component,
            operation="controller_state",
        )

    def close(self) -> None:
        if self._closed:
            return
        backend = self._backend
        self._closed = True
        if backend is None:
            return
        close = getattr(backend, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                raise AdapterError(
                    "indy_close_error",
                    f"IndyDCP3 close failed: {exc}",
                    component=self.component,
                    operation="close",
                ) from exc


__all__ = ["IndyDcp3Adapter"]
