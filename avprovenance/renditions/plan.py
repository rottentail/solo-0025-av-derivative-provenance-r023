"""Validate and normalize media plans before any lifecycle mutation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..errors import PlanInvalid
from ..serialization import digest_json
from ..validation import (
    require_integer,
    require_number,
    require_object,
    require_string,
)


MAX_OPERATIONS = 12
PAIR_INTERVAL_MIN_SECONDS = 0.001
PAIR_INTERVAL_MAX_SECONDS = 600.0


@dataclass(frozen=True)
class CompiledPlan:
    operations: list[dict[str, Any]]
    digest: str
    operation_types: list[str]


@dataclass(frozen=True)
class CompiledPairPlan:
    audio_operations: list[dict[str, Any]]
    video_operations: list[dict[str, Any]]
    digest: str
    operation_types: list[str]


@dataclass(frozen=True)
class CompiledMemberPlan:
    """A shared cut requested through one pair member.

    ``trim`` is either ``{start_seconds, duration_seconds}`` for an audio
    member or ``{start_frame, frame_count}`` for a video member.  Remaining
    operations apply to the requesting member only.
    """

    media_kind: str
    trim: dict[str, Any]
    remaining_operations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def operation_types(self) -> list[str]:
        return [str(item["type"]) for item in self.remaining_operations]


def _only_keys(operation: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(operation) - allowed)
    if unknown:
        raise PlanInvalid(
            f"{name} contains unsupported fields.",
            details={"fields": unknown},
        )


def _audio_trim(operation: dict[str, Any]) -> dict[str, Any]:
    _only_keys(
        operation,
        {"type", "start_seconds", "duration_seconds"},
        "media_trim",
    )
    return {
        "type": "media_trim",
        "start_seconds": require_number(
            operation.get("start_seconds"),
            "start_seconds",
            minimum=0,
            maximum=600,
        ),
        "duration_seconds": require_number(
            operation.get("duration_seconds"),
            "duration_seconds",
            minimum=0.001,
            maximum=600,
        ),
    }


def _audio_gain(operation: dict[str, Any]) -> dict[str, Any]:
    _only_keys(operation, {"type", "db"}, "audio_gain")
    return {
        "type": "audio_gain",
        "db": require_number(
            operation.get("db"),
            "db",
            minimum=-48,
            maximum=24,
        ),
    }


def _audio_normalize(operation: dict[str, Any]) -> dict[str, Any]:
    _only_keys(operation, {"type", "target_peak"}, "audio_normalize")
    return {
        "type": "audio_normalize",
        "target_peak": require_number(
            operation.get("target_peak"),
            "target_peak",
            minimum=0.05,
            maximum=1.0,
        ),
    }


def _audio_mix(
    operation: dict[str, Any],
    *,
    has_secondary: bool,
) -> dict[str, Any]:
    _only_keys(
        operation,
        {
            "type",
            "secondary_gain",
            "primary_weight",
            "secondary_weight",
        },
        "audio_mix",
    )
    if not has_secondary:
        raise PlanInvalid("audio_mix requires secondary_asset_id.")
    return {
        "type": "audio_mix",
        "secondary_gain": require_number(
            operation.get("secondary_gain"),
            "secondary_gain",
            minimum=-48,
            maximum=24,
        ),
        "primary_weight": require_number(
            operation.get("primary_weight"),
            "primary_weight",
            minimum=0,
            maximum=1.5,
        ),
        "secondary_weight": require_number(
            operation.get("secondary_weight"),
            "secondary_weight",
            minimum=0,
            maximum=1.5,
        ),
    }


def _video_trim(operation: dict[str, Any]) -> dict[str, Any]:
    _only_keys(
        operation,
        {"type", "start_frame", "frame_count"},
        "media_trim",
    )
    return {
        "type": "media_trim",
        "start_frame": require_integer(
            operation.get("start_frame"),
            "start_frame",
            minimum=0,
            maximum=239,
        ),
        "frame_count": require_integer(
            operation.get("frame_count"),
            "frame_count",
            minimum=1,
            maximum=240,
        ),
    }


def _video_crop(operation: dict[str, Any]) -> dict[str, Any]:
    _only_keys(
        operation,
        {"type", "x", "y", "width", "height"},
        "video_crop",
    )
    values = {
        "type": "video_crop",
        "x": require_integer(operation.get("x"), "x", minimum=0, maximum=1918),
        "y": require_integer(operation.get("y"), "y", minimum=0, maximum=1078),
        "width": require_integer(
            operation.get("width"),
            "width",
            minimum=2,
            maximum=1920,
        ),
        "height": require_integer(
            operation.get("height"),
            "height",
            minimum=2,
            maximum=1080,
        ),
    }
    if any(values[key] % 2 for key in ("x", "y", "width", "height")):
        raise PlanInvalid("video_crop geometry must use even values.")
    return values


def _video_luma(operation: dict[str, Any]) -> dict[str, Any]:
    _only_keys(operation, {"type", "amount"}, "video_luma_offset")
    return {
        "type": "video_luma_offset",
        "amount": require_integer(
            operation.get("amount"),
            "amount",
            minimum=-64,
            maximum=64,
        ),
    }


def _video_mirror(operation: dict[str, Any]) -> dict[str, Any]:
    _only_keys(operation, {"type"}, "video_mirror")
    return {"type": "video_mirror"}


def _operation_validator(
    operation_type: str,
    media_kind: str,
) -> Callable[[dict[str, Any]], dict[str, Any]] | None:
    if media_kind == "audio":
        validators = {
            "media_trim": _audio_trim,
            "audio_gain": _audio_gain,
            "audio_normalize": _audio_normalize,
            "audio_mix": _audio_mix,
        }
    else:
        validators = {
            "media_trim": _video_trim,
            "video_crop": _video_crop,
            "video_luma_offset": _video_luma,
            "video_mirror": _video_mirror,
        }
    return validators.get(operation_type)


def compile_plan(
    media_kind: str,
    raw_operations: Any,
    *,
    has_secondary: bool,
) -> CompiledPlan:
    if media_kind not in {"audio", "video"}:
        raise PlanInvalid("The primary media kind is not supported.")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise PlanInvalid("operations must be a non-empty array.")
    if len(raw_operations) > MAX_OPERATIONS:
        raise PlanInvalid(
            "The plan contains too many operations.",
            details={"maximum": MAX_OPERATIONS},
        )

    normalized: list[dict[str, Any]] = []
    mixed = False
    for index, raw in enumerate(raw_operations):
        operation = require_object(raw, f"operations[{index}]")
        operation_type = require_string(
            operation.get("type"),
            f"operations[{index}].type",
            maximum=64,
        )
        if media_kind == "audio" and operation_type == "audio_mix":
            if mixed:
                raise PlanInvalid("audio_mix may appear only once.")
            normalized.append(_audio_mix(operation, has_secondary=has_secondary))
            mixed = True
            continue

        validator = _operation_validator(operation_type, media_kind)
        if validator is None:
            raise PlanInvalid(
                f"Operation {operation_type!r} is not valid for {media_kind} media.",
                details={"index": index},
            )
        normalized.append(validator(operation))

    if media_kind == "video" and has_secondary:
        raise PlanInvalid("Video plans do not accept a secondary asset.")

    return CompiledPlan(
        operations=normalized,
        digest=digest_json({"media_kind": media_kind, "operations": normalized}),
        operation_types=[item["type"] for item in normalized],
    )


def compile_pair_plan(raw_plan: Any) -> CompiledPairPlan:
    """Validate the audio and video operation lists of a new pair.

    A pair plan carries no ``media_trim``: the declared sync points and shared
    interval are the pairing mechanism, so an inline trim would silently
    desynchronize a member.  ``audio_mix`` is also refused because a pair owns
    exactly one audio source.
    """

    plan = require_object(raw_plan, "operations")
    _only_keys(plan, {"audio", "video"}, "operations")
    audio_operations = _pair_member_operations(
        plan.get("audio"),
        "audio",
        "operations.audio",
    )
    video_operations = _pair_member_operations(
        plan.get("video"),
        "video",
        "operations.video",
    )
    return CompiledPairPlan(
        audio_operations=audio_operations,
        video_operations=video_operations,
        digest=digest_json({"audio": audio_operations, "video": video_operations}),
        operation_types=[
            *(f"audio:{item['type']}" for item in audio_operations),
            *(f"video:{item['type']}" for item in video_operations),
        ],
    )


def _pair_member_operations(
    raw_operations: Any,
    media_kind: str,
    name: str,
) -> list[dict[str, Any]]:
    if raw_operations is None:
        return []
    if not isinstance(raw_operations, list):
        raise PlanInvalid(f"{name} must be an array.")
    if len(raw_operations) > MAX_OPERATIONS:
        raise PlanInvalid(
            f"{name} contains too many operations.",
            details={"maximum": MAX_OPERATIONS},
        )
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_operations):
        operation = require_object(raw, f"{name}[{index}]")
        operation_type = require_string(
            operation.get("type"),
            f"{name}[{index}].type",
            maximum=64,
        )
        if operation_type == "media_trim":
            raise PlanInvalid(
                "Pair plans use the declared interval instead of media_trim.",
                details={"index": index},
            )
        if operation_type == "audio_mix":
            raise PlanInvalid("Pair plans do not accept a secondary mix source.")
        validator = _operation_validator(operation_type, media_kind)
        if validator is None:
            raise PlanInvalid(
                f"Operation {operation_type!r} is not valid for {media_kind} media.",
                details={"index": index},
            )
        normalized.append(validator(operation))
    return normalized


def compile_member_plan(
    media_kind: str,
    raw_operations: Any,
) -> CompiledMemberPlan:
    """Validate a shared cut requested through an existing pair member.

    The request must contain exactly one ``media_trim``; that trim defines the
    common interval for both members.  Audio members name seconds, video
    members name frames.  Other operations stay with the requesting member.
    """

    if media_kind not in {"audio", "video"}:
        raise PlanInvalid("The member media kind is not supported.")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise PlanInvalid("operations must be a non-empty array.")
    trims: list[dict[str, Any]] = []
    remaining_raw: list[Any] = []
    for index, raw in enumerate(raw_operations):
        operation = require_object(raw, f"operations[{index}]")
        operation_type = require_string(
            operation.get("type"),
            f"operations[{index}].type",
            maximum=64,
        )
        if operation_type == "media_trim":
            trims.append(operation)
        else:
            remaining_raw.append(operation)
    if not trims:
        raise PlanInvalid(
            "A pair member derivation must include exactly one media_trim."
        )
    if len(trims) > 1:
        raise PlanInvalid("A pair member derivation accepts one media_trim.")

    trim = trims[0]
    if media_kind == "audio":
        _only_keys(trim, {"type", "start_seconds", "duration_seconds"}, "media_trim")
        normalized_trim = {
            "start_seconds": require_number(
                trim.get("start_seconds", 0.0),
                "start_seconds",
                minimum=0,
                maximum=PAIR_INTERVAL_MAX_SECONDS,
            ),
            "duration_seconds": require_number(
                trim.get("duration_seconds"),
                "duration_seconds",
                minimum=PAIR_INTERVAL_MIN_SECONDS,
                maximum=PAIR_INTERVAL_MAX_SECONDS,
            ),
        }
    else:
        _only_keys(trim, {"type", "start_frame", "frame_count"}, "media_trim")
        normalized_trim = {
            "start_frame": require_integer(
                trim.get("start_frame", 0),
                "start_frame",
                minimum=0,
                maximum=239,
            ),
            "frame_count": require_integer(
                trim.get("frame_count"),
                "frame_count",
                minimum=1,
                maximum=240,
            ),
        }

    remaining = _pair_member_operations(remaining_raw, media_kind, "operations")
    return CompiledMemberPlan(
        media_kind=media_kind,
        trim=normalized_trim,
        remaining_operations=remaining,
    )


def compile_interval(raw_interval: Any) -> dict[str, float]:
    """Validate the caller-declared shared playback interval of a new pair."""

    interval = require_object(raw_interval, "interval")
    _only_keys(interval, {"start_seconds", "duration_seconds"}, "interval")
    return {
        "start_seconds": require_number(
            interval.get("start_seconds", 0.0),
            "interval.start_seconds",
            minimum=0,
            maximum=PAIR_INTERVAL_MAX_SECONDS,
        ),
        "duration_seconds": require_number(
            interval.get("duration_seconds"),
            "interval.duration_seconds",
            minimum=PAIR_INTERVAL_MIN_SECONDS,
            maximum=PAIR_INTERVAL_MAX_SECONDS,
        ),
    }


def compile_sync(raw_sync: Any) -> dict[str, float]:
    """Validate the declared alignment of the two sources at pair time zero."""

    sync = require_object(raw_sync, "sync")
    _only_keys(sync, {"audio_seconds", "video_seconds"}, "sync")
    return {
        "audio_seconds": require_number(
            sync.get("audio_seconds", 0.0),
            "sync.audio_seconds",
            minimum=0,
            maximum=PAIR_INTERVAL_MAX_SECONDS,
        ),
        "video_seconds": require_number(
            sync.get("video_seconds", 0.0),
            "sync.video_seconds",
            minimum=0,
            maximum=PAIR_INTERVAL_MAX_SECONDS,
        ),
    }
