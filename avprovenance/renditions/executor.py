"""Execute a normalized plan and convert media failures to domain errors."""

from __future__ import annotations

from typing import Any

from ..errors import RenderFailed, UnsupportedMedia
from ..media import (
    MediaOperationFailure,
    PairedRenderOutcome,
    RenderOutcome,
    apply_media_operations,
    render_pair,
)
from ..media.timeline import PairTrims


def execute_plan(
    primary_bytes: bytes,
    operations: list[dict[str, Any]],
    secondary_bytes: bytes | None,
) -> RenderOutcome:
    try:
        return apply_media_operations(primary_bytes, operations, secondary_bytes)
    except MediaOperationFailure as exc:
        raise RenderFailed(
            "The rendition could not be produced.",
            details={"reason": exc.code, "detail": exc.message},
        ) from exc
    except UnsupportedMedia as exc:
        raise RenderFailed(
            "The rendition produced unsupported media bytes.",
            details={"reason": exc.code},
        ) from exc


def execute_pair(
    *,
    audio_bytes: bytes,
    video_bytes: bytes,
    trims: PairTrims,
    audio_operations: list[dict[str, Any]],
    video_operations: list[dict[str, Any]],
) -> PairedRenderOutcome:
    try:
        return render_pair(
            audio_bytes=audio_bytes,
            audio_start_sample=trims.audio.start,
            audio_sample_count=trims.audio.count,
            video_bytes=video_bytes,
            video_frame_start=trims.video.start,
            video_frame_count=trims.video.count,
            audio_operations=audio_operations,
            video_operations=video_operations,
        )
    except MediaOperationFailure as exc:
        raise RenderFailed(
            "The media pair could not be produced.",
            details={"reason": exc.code, "detail": exc.message},
        ) from exc
    except UnsupportedMedia as exc:
        raise RenderFailed(
            "The media pair produced unsupported media bytes.",
            details={"reason": exc.code},
        ) from exc
