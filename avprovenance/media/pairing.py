"""Render one paired WAV and Y4M output under a shared playback interval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import UnsupportedMedia
from ..models import MediaDescriptor
from .audio_ops import apply_audio_operations, trim_audio_range
from .errors import MediaOperationFailure
from .inspector import inspect_media
from .video_ops import apply_video_operations, trim_video_range
from .wav import decode_wav, encode_wav
from .y4m import decode_y4m, encode_y4m


@dataclass(frozen=True)
class PairedRenderOutcome:
    audio_data: bytes
    audio_descriptor: MediaDescriptor
    video_data: bytes
    video_descriptor: MediaDescriptor
    audio_sample_count: int
    video_frame_count: int


def _check_source_kind(
    bytes_value: bytes,
    expected_kind: str,
    label: str,
) -> MediaDescriptor:
    descriptor = inspect_media(bytes_value)
    if descriptor.kind != expected_kind:
        raise MediaOperationFailure(
            "PAIR_MEDIA_KIND_MISMATCH",
            f"The {label} source is not {expected_kind} media.",
        )
    return descriptor


def render_pair(
    *,
    audio_bytes: bytes,
    audio_start_sample: int,
    audio_sample_count: int,
    video_bytes: bytes,
    video_frame_start: int,
    video_frame_count: int,
    audio_operations: list[dict[str, Any]],
    video_operations: list[dict[str, Any]],
) -> PairedRenderOutcome:
    """Trim both members to the shared interval and apply their own plans.

    Both byte streams are produced before the caller persists anything, so a
    failure on either side cannot leave a single successful member behind.
    """

    _check_source_kind(audio_bytes, "audio", "audio")
    _check_source_kind(video_bytes, "video", "video")

    audio_clip = trim_audio_range(
        decode_wav(audio_bytes),
        audio_start_sample,
        audio_sample_count,
    )
    audio_clip = apply_audio_operations(audio_clip, audio_operations)
    video_clip = trim_video_range(
        decode_y4m(video_bytes),
        video_frame_start,
        video_frame_count,
    )
    video_clip = apply_video_operations(video_clip, video_operations)

    audio_bytes_out = encode_wav(audio_clip)
    video_bytes_out = encode_y4m(video_clip)
    audio_descriptor = inspect_media(audio_bytes_out)
    video_descriptor = inspect_media(video_bytes_out)
    if audio_descriptor.kind != "audio" or video_descriptor.kind != "video":
        raise UnsupportedMedia("A paired render changed a member media kind.")
    return PairedRenderOutcome(
        audio_data=audio_bytes_out,
        audio_descriptor=audio_descriptor,
        video_data=video_bytes_out,
        video_descriptor=video_descriptor,
        audio_sample_count=audio_clip.frame_count,
        video_frame_count=video_clip.frame_count,
    )


__all__ = ["PairedRenderOutcome", "render_pair"]
