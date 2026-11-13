"""Recompute a completed rendition and compare it with recorded output."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import RenderFailed, UnsupportedMedia
from ..media import (
    MediaOperationFailure,
    apply_media_operations,
    render_pair,
)
from ..media.timeline import PairTrims
from ..models import MediaAsset
from ..serialization import sha256_hex


@dataclass(frozen=True)
class VerificationOutcome:
    status: str
    expected_digest: str
    reproduced_digest: str | None
    expected_descriptor: dict[str, Any]
    reproduced_descriptor: dict[str, Any] | None
    reason: str | None
    members: list[dict[str, Any]] = field(default_factory=list)


def _member_outcome(
    *,
    role: str,
    expected_asset: MediaAsset,
    recorded_bytes: bytes,
    reproduced_bytes: bytes | None,
    reproduced_descriptor: dict[str, Any] | None,
    reason: str | None,
) -> dict[str, Any]:
    expected_digest = expected_asset.sha256
    expected_descriptor = expected_asset.descriptor.to_dict()
    return {
        "role": role,
        "status": "verified" if reason is None else "mismatch",
        "expected_digest": expected_digest,
        "reproduced_digest": (
            sha256_hex(reproduced_bytes) if reproduced_bytes is not None else None
        ),
        "expected_descriptor": expected_descriptor,
        "reproduced_descriptor": reproduced_descriptor,
        "reason": reason,
    }


def reproduce_output(
    *,
    output_asset: MediaAsset,
    recorded_output_bytes: bytes,
    primary_bytes: bytes,
    operations: list[dict[str, Any]],
    secondary_bytes: bytes | None,
) -> VerificationOutcome:
    expected_descriptor = output_asset.descriptor.to_dict()
    expected_digest = output_asset.sha256
    recorded_digest = sha256_hex(recorded_output_bytes)
    if recorded_digest != expected_digest:
        return VerificationOutcome(
            status="mismatch",
            expected_digest=expected_digest,
            reproduced_digest=None,
            expected_descriptor=expected_descriptor,
            reproduced_descriptor=None,
            reason="recorded_output_digest_drift",
        )

    try:
        outcome = apply_media_operations(
            primary_bytes,
            operations,
            secondary_bytes,
        )
    except (MediaOperationFailure, UnsupportedMedia, RenderFailed) as exc:
        reason = getattr(exc, "code", "reproduction_failed")
        return VerificationOutcome(
            status="mismatch",
            expected_digest=expected_digest,
            reproduced_digest=None,
            expected_descriptor=expected_descriptor,
            reproduced_descriptor=None,
            reason=str(reason),
        )

    reproduced_digest = sha256_hex(outcome.data)
    reproduced_descriptor = outcome.descriptor.to_dict()
    if reproduced_digest != expected_digest:
        return VerificationOutcome(
            status="mismatch",
            expected_digest=expected_digest,
            reproduced_digest=reproduced_digest,
            expected_descriptor=expected_descriptor,
            reproduced_descriptor=reproduced_descriptor,
            reason="reproduced_bytes_differ",
        )
    if reproduced_descriptor != expected_descriptor:
        return VerificationOutcome(
            status="mismatch",
            expected_digest=expected_digest,
            reproduced_digest=reproduced_digest,
            expected_descriptor=expected_descriptor,
            reproduced_descriptor=reproduced_descriptor,
            reason="reproduced_descriptor_differs",
        )
    return VerificationOutcome(
        status="verified",
        expected_digest=expected_digest,
        reproduced_digest=reproduced_digest,
        expected_descriptor=expected_descriptor,
        reproduced_descriptor=reproduced_descriptor,
        reason=None,
    )


def reproduce_pair(
    *,
    audio_asset: MediaAsset,
    recorded_audio_bytes: bytes,
    video_asset: MediaAsset,
    recorded_video_bytes: bytes,
    audio_source_bytes: bytes,
    video_source_bytes: bytes,
    trims: PairTrims,
    audio_operations: list[dict[str, Any]],
    video_operations: list[dict[str, Any]],
) -> VerificationOutcome:
    """Replay both pair members and compare each against its recorded output."""

    audio_expected = audio_asset.descriptor.to_dict()
    video_expected = video_asset.descriptor.to_dict()
    members_reason: str | None = None
    if sha256_hex(recorded_audio_bytes) != audio_asset.sha256:
        members_reason = "recorded_audio_digest_drift"
    elif sha256_hex(recorded_video_bytes) != video_asset.sha256:
        members_reason = "recorded_video_digest_drift"
    if members_reason is not None:
        return VerificationOutcome(
            status="mismatch",
            expected_digest=audio_asset.sha256,
            reproduced_digest=None,
            expected_descriptor=audio_expected,
            reproduced_descriptor=None,
            reason=members_reason,
            members=[
                _member_outcome(
                    role="audio",
                    expected_asset=audio_asset,
                    recorded_bytes=recorded_audio_bytes,
                    reproduced_bytes=None,
                    reproduced_descriptor=None,
                    reason=members_reason,
                ),
                _member_outcome(
                    role="video",
                    expected_asset=video_asset,
                    recorded_bytes=recorded_video_bytes,
                    reproduced_bytes=None,
                    reproduced_descriptor=None,
                    reason=members_reason,
                ),
            ],
        )

    try:
        outcome = render_pair(
            audio_bytes=audio_source_bytes,
            audio_start_sample=trims.audio.start,
            audio_sample_count=trims.audio.count,
            video_bytes=video_source_bytes,
            video_frame_start=trims.video.start,
            video_frame_count=trims.video.count,
            audio_operations=audio_operations,
            video_operations=video_operations,
        )
    except (MediaOperationFailure, UnsupportedMedia, RenderFailed) as exc:
        reason = str(getattr(exc, "code", "reproduction_failed"))
        return VerificationOutcome(
            status="mismatch",
            expected_digest=audio_asset.sha256,
            reproduced_digest=None,
            expected_descriptor=audio_expected,
            reproduced_descriptor=None,
            reason=reason,
            members=[
                _member_outcome(
                    role="audio",
                    expected_asset=audio_asset,
                    recorded_bytes=recorded_audio_bytes,
                    reproduced_bytes=None,
                    reproduced_descriptor=None,
                    reason=reason,
                ),
                _member_outcome(
                    role="video",
                    expected_asset=video_asset,
                    recorded_bytes=recorded_video_bytes,
                    reproduced_bytes=None,
                    reproduced_descriptor=None,
                    reason=reason,
                ),
            ],
        )

    audio_reproduced_descriptor = outcome.audio_descriptor.to_dict()
    video_reproduced_descriptor = outcome.video_descriptor.to_dict()
    audio_reason = None
    video_reason = None
    if sha256_hex(outcome.audio_data) != audio_asset.sha256:
        audio_reason = "reproduced_audio_bytes_differ"
    elif audio_reproduced_descriptor != audio_expected:
        audio_reason = "reproduced_audio_descriptor_differs"
    if sha256_hex(outcome.video_data) != video_asset.sha256:
        video_reason = "reproduced_video_bytes_differ"
    elif video_reproduced_descriptor != video_expected:
        video_reason = "reproduced_video_descriptor_differs"

    members = [
        _member_outcome(
            role="audio",
            expected_asset=audio_asset,
            recorded_bytes=recorded_audio_bytes,
            reproduced_bytes=outcome.audio_data,
            reproduced_descriptor=audio_reproduced_descriptor,
            reason=audio_reason,
        ),
        _member_outcome(
            role="video",
            expected_asset=video_asset,
            recorded_bytes=recorded_video_bytes,
            reproduced_bytes=outcome.video_data,
            reproduced_descriptor=video_reproduced_descriptor,
            reason=video_reason,
        ),
    ]
    if audio_reason or video_reason:
        return VerificationOutcome(
            status="mismatch",
            expected_digest=audio_asset.sha256,
            reproduced_digest=sha256_hex(outcome.audio_data),
            expected_descriptor=audio_expected,
            reproduced_descriptor=audio_reproduced_descriptor,
            reason=audio_reason or video_reason,
            members=members,
        )
    return VerificationOutcome(
        status="verified",
        expected_digest=audio_asset.sha256,
        reproduced_digest=sha256_hex(outcome.audio_data),
        expected_descriptor=audio_expected,
        reproduced_descriptor=audio_reproduced_descriptor,
        reason=None,
        members=members,
    )
