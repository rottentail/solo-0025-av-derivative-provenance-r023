"""Exact rational timeline math for paired WAV and Y4M derivations.

Pair timing is tracked with reduced :class:`fractions.Fraction` values instead
of floating-point seconds.  Each delivery records, for both members, the exact
*requested* position in the original source and the quantized position that was
actually delivered.  Later cuts add their offset to the exact requested
positions before quantizing again, so the delivered position stays within half
an audio sample and under one video frame of the request no matter how many
times a delivery is cut.  Relative audio/video alignment therefore stays
bounded and never accumulates rounding error.

A video cut named in native frames is anchored to the parent video's delivered
frame grid, which keeps it exactly frame aligned while the same exact offset
is applied to the audio member.  This module has no dependency on the
application or storage layers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any


@dataclass(frozen=True)
class MemberTrim:
    """One member's slice: a start index and length inside its immediate parent."""

    start: int
    count: int


@dataclass(frozen=True)
class PairTrims:
    """Both members' slices plus the exact requested interval.

    ``start`` and ``duration`` are pair-relative seconds against the immediate
    parent outputs.  ``audio_requested`` and ``video_requested`` are exact
    absolute positions in the original source.
    """

    audio: MemberTrim
    video: MemberTrim
    start: Fraction
    duration: Fraction
    audio_requested: Fraction
    video_requested: Fraction


@dataclass(frozen=True)
class PairTimeline:
    """Cumulative member positions in the original source for one delivery."""

    audio_delivered: Fraction
    video_delivered: Fraction
    audio_requested: Fraction
    video_requested: Fraction
    generation: int

    @property
    def audio_drift(self) -> Fraction:
        return self.audio_delivered - self.audio_requested

    @property
    def video_drift(self) -> Fraction:
        return self.video_delivered - self.video_requested

    @property
    def relative_drift(self) -> Fraction:
        return self.audio_drift - self.video_drift


def decimal_fraction(value: Any) -> Fraction:
    """Convert a JSON number to the exact decimal rational it was written as."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("value must be numeric")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("value must be finite")
    return Fraction(str(value))


def round_half_up(value: Fraction) -> int:
    """Round a rational to the nearest integer, halves toward positive infinity."""

    return math.floor(value + Fraction(1, 2))


def frame_rate(rate_numerator: int, rate_denominator: int) -> Fraction:
    return Fraction(int(rate_numerator), int(rate_denominator))


def frame_offset(
    frame_count: int,
    rate_numerator: int,
    rate_denominator: int,
) -> Fraction:
    """Exact duration of an integer number of frames at a rational rate."""

    return Fraction(
        int(frame_count) * int(rate_denominator),
        int(rate_numerator),
    )


def _audio_count(duration: Fraction, audio_rate: int) -> int:
    return max(1, round_half_up(Fraction(duration) * int(audio_rate)))


def _video_end_count(
    *,
    video_requested: Fraction,
    duration: Fraction,
    rate: Fraction,
    delivered_frame: int,
) -> int:
    """Frames from ``delivered_frame`` through the requested end."""

    end = math.ceil((Fraction(video_requested) + Fraction(duration)) * rate)
    return max(1, end - int(delivered_frame))


def _trims(
    *,
    audio_index: int,
    video_index: int,
    audio_count: int,
    video_count: int,
    start: Fraction,
    duration: Fraction,
    audio_requested: Fraction,
    video_requested: Fraction,
) -> PairTrims:
    return PairTrims(
        audio=MemberTrim(start=int(audio_index), count=max(1, int(audio_count))),
        video=MemberTrim(start=int(video_index), count=max(1, int(video_count))),
        start=Fraction(start),
        duration=Fraction(duration),
        audio_requested=Fraction(audio_requested),
        video_requested=Fraction(video_requested),
    )


def origin_trims(
    *,
    audio_sync: Fraction,
    video_sync: Fraction,
    start: Fraction,
    duration: Fraction,
    audio_rate: int,
    rate_numerator: int,
    rate_denominator: int,
) -> PairTrims:
    """Quantize the caller-declared sync points and shared interval.

    ``audio_sync`` and ``video_sync`` are the positions in each source that
    line up at pair time zero.  Audio begins on the nearest sample; video begins
    on the frame containing the requested position and covers the requested
    end, including a partially overlapped final frame.
    """

    rate = frame_rate(rate_numerator, rate_denominator)
    audio_requested = Fraction(audio_sync) + Fraction(start)
    video_requested = Fraction(video_sync) + Fraction(start)

    audio_index = round_half_up(audio_requested * int(audio_rate))
    video_index = math.floor(video_requested * rate)
    return _trims(
        audio_index=audio_index,
        video_index=video_index,
        audio_count=_audio_count(duration, audio_rate),
        video_count=_video_end_count(
            video_requested=video_requested,
            duration=duration,
            rate=rate,
            delivered_frame=video_index,
        ),
        start=start,
        duration=duration,
        audio_requested=audio_requested,
        video_requested=video_requested,
    )


def timeline_for(
    trims: PairTrims,
    *,
    generation: int,
    audio_rate: int,
    rate_numerator: int,
    rate_denominator: int,
) -> PairTimeline:
    """Absolute delivered positions implied by one delivery's request."""

    rate = frame_rate(rate_numerator, rate_denominator)
    return PairTimeline(
        audio_delivered=Fraction(
            round_half_up(trims.audio_requested * int(audio_rate)),
            int(audio_rate),
        ),
        video_delivered=Fraction(
            math.floor(trims.video_requested * rate), 1
        )
        / rate,
        audio_requested=trims.audio_requested,
        video_requested=trims.video_requested,
        generation=int(generation),
    )


def timeline_after_parent(
    parent: PairTimeline,
    trims: PairTrims,
    *,
    audio_rate: int,
    rate_numerator: int,
    rate_denominator: int,
) -> PairTimeline:
    """Advance a parent timeline by a child cut's delivered indices.

    The child's ``requested`` fields already hold the absolute original-source
    positions; only the delivered positions need to be computed from the
    parent's delivered positions plus the child's slice offsets.
    """

    rate = frame_rate(rate_numerator, rate_denominator)
    return PairTimeline(
        audio_delivered=parent.audio_delivered
        + Fraction(trims.audio.start, int(audio_rate)),
        video_delivered=parent.video_delivered
        + Fraction(trims.video.start, 1) / rate,
        audio_requested=trims.audio_requested,
        video_requested=trims.video_requested,
        generation=parent.generation + 1,
    )


def cut_from_audio(
    parent: PairTimeline,
    *,
    offset: Fraction,
    duration: Fraction,
    audio_rate: int,
    rate_numerator: int,
    rate_denominator: int,
) -> PairTrims:
    """Cut both members by an exact offset named in audio seconds."""

    rate = frame_rate(rate_numerator, rate_denominator)
    audio_requested = parent.audio_requested + Fraction(offset)
    video_requested = parent.video_requested + Fraction(offset)

    parent_audio_index = round_half_up(parent.audio_requested * int(audio_rate))
    parent_video_index = math.floor(parent.video_requested * rate)
    audio_index = round_half_up(audio_requested * int(audio_rate)) - parent_audio_index
    video_index = math.floor(video_requested * rate) - parent_video_index
    return _trims(
        audio_index=audio_index,
        video_index=video_index,
        audio_count=_audio_count(duration, audio_rate),
        video_count=_video_end_count(
            video_requested=video_requested,
            duration=duration,
            rate=rate,
            delivered_frame=math.floor(video_requested * rate),
        ),
        start=offset,
        duration=duration,
        audio_requested=audio_requested,
        video_requested=video_requested,
    )


def cut_from_video(
    parent: PairTimeline,
    *,
    start_frame: int,
    frame_count: int,
    audio_rate: int,
    rate_numerator: int,
    rate_denominator: int,
) -> PairTrims:
    """Cut both members by an offset named in the parent video's native frames.

    The offset is exact and is applied to both members' *requested* positions,
    so the pair keeps one shared request.  Video then quantizes that request
    down to its frame grid, which delivers exactly the named frames whenever
    the parent request was already frame aligned.
    """

    rate = frame_rate(rate_numerator, rate_denominator)
    offset = frame_offset(start_frame, rate_numerator, rate_denominator)
    duration = frame_offset(frame_count, rate_numerator, rate_denominator)
    video_requested = parent.video_requested + offset
    audio_requested = parent.audio_requested + offset

    parent_audio_index = round_half_up(parent.audio_requested * int(audio_rate))
    parent_video_index = math.floor(parent.video_requested * rate)
    audio_index = round_half_up(audio_requested * int(audio_rate)) - parent_audio_index
    video_index = math.floor(video_requested * rate) - parent_video_index
    return _trims(
        audio_index=audio_index,
        video_index=video_index,
        audio_count=_audio_count(duration, audio_rate),
        video_count=_video_end_count(
            video_requested=video_requested,
            duration=duration,
            rate=rate,
            delivered_frame=math.floor(video_requested * rate),
        ),
        start=offset,
        duration=duration,
        audio_requested=audio_requested,
        video_requested=video_requested,
    )


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def trims_to_dict(trims: PairTrims) -> dict[str, Any]:
    return {
        "start_seconds": float(trims.start),
        "duration_seconds": float(trims.duration),
        "audio": {
            "sample_start": trims.audio.start,
            "sample_count": trims.audio.count,
        },
        "video": {
            "frame_start": trims.video.start,
            "frame_count": trims.video.count,
        },
        "exact": {
            "start": _fraction_text(trims.start),
            "duration": _fraction_text(trims.duration),
            "audio_requested_seconds": _fraction_text(trims.audio_requested),
            "video_requested_seconds": _fraction_text(trims.video_requested),
        },
    }


def trims_from_dict(value: dict[str, Any]) -> PairTrims:
    exact = dict(value.get("exact") or {})
    audio = dict(value["audio"])
    video = dict(value["video"])

    def read(key: str, fallback: Fraction) -> Fraction:
        text = exact.get(key)
        if text is None:
            return fallback
        return Fraction(str(text))

    start = Fraction(str(exact.get("start", value["start_seconds"])))
    duration = Fraction(str(exact.get("duration", value["duration_seconds"])))
    return PairTrims(
        audio=MemberTrim(
            start=int(audio["sample_start"]),
            count=int(audio["sample_count"]),
        ),
        video=MemberTrim(
            start=int(video["frame_start"]),
            count=int(video["frame_count"]),
        ),
        start=start,
        duration=duration,
        audio_requested=read("audio_requested_seconds", start),
        video_requested=read("video_requested_seconds", start),
    )


def timeline_to_dict(timeline: PairTimeline) -> dict[str, Any]:
    return {
        "generation": timeline.generation,
        "audio_source_start_seconds": float(timeline.audio_delivered),
        "video_source_start_seconds": float(timeline.video_delivered),
        "audio_requested_seconds": float(timeline.audio_requested),
        "video_requested_seconds": float(timeline.video_requested),
        "audio_drift_seconds": float(timeline.audio_drift),
        "video_drift_seconds": float(timeline.video_drift),
        "exact": {
            "audio_delivered": _fraction_text(timeline.audio_delivered),
            "video_delivered": _fraction_text(timeline.video_delivered),
            "audio_requested": _fraction_text(timeline.audio_requested),
            "video_requested": _fraction_text(timeline.video_requested),
        },
    }


def timeline_from_dict(value: dict[str, Any]) -> PairTimeline:
    exact = dict(value.get("exact") or {})

    def read(key: str, fallback: str) -> Fraction:
        text = exact.get(key)
        if text is None:
            return Fraction(str(value[fallback]))
        return Fraction(str(text))

    return PairTimeline(
        audio_delivered=read("audio_delivered", "audio_source_start_seconds"),
        video_delivered=read("video_delivered", "video_source_start_seconds"),
        audio_requested=read("audio_requested", "audio_requested_seconds"),
        video_requested=read("video_requested", "video_requested_seconds"),
        generation=int(value.get("generation", 0)),
    )


def interval_document(
    trims: PairTrims,
    *,
    timeline: PairTimeline,
    audio_rate: int,
    rate_numerator: int,
    rate_denominator: int,
    sync: dict[str, float] | None = None,
) -> dict[str, Any]:
    """One interval representation shared by the pair and both members.

    Top-level ``start_seconds``/``duration_seconds`` are pair-relative against
    the immediate parents.  ``members`` restates each member on its own
    playback timeline; ``timeline`` maps both members back to the original
    sources so replaying either file explains the same interval.  Audio and
    video share the pair-relative start and duration; only their quantization
    differs.
    """

    rate = frame_rate(rate_numerator, rate_denominator)
    document = trims_to_dict(trims)
    # ``start_seconds``/``duration_seconds`` restate the one shared interval so
    # both member blocks and the top level agree on what was requested.
    # ``delivered_*`` describe where each medium actually landed.
    document["members"] = {
        "audio": {
            "start_seconds": float(trims.start),
            "duration_seconds": float(trims.duration),
            "delivered_start_seconds": float(
                Fraction(trims.audio.start, int(audio_rate))
            ),
            "delivered_duration_seconds": float(
                Fraction(trims.audio.count, int(audio_rate))
            ),
            "source_start_seconds": float(timeline.audio_delivered),
            "sample_start": trims.audio.start,
            "sample_count": trims.audio.count,
            "sample_rate": int(audio_rate),
            "quantized": (
                Fraction(trims.audio.start, int(audio_rate)) != trims.start
                or Fraction(trims.audio.count, int(audio_rate)) != trims.duration
            ),
        },
        "video": {
            "start_seconds": float(trims.start),
            "duration_seconds": float(trims.duration),
            "delivered_start_seconds": float(Fraction(trims.video.start, 1) / rate),
            "delivered_duration_seconds": float(Fraction(trims.video.count, 1) / rate),
            "source_start_seconds": float(timeline.video_delivered),
            "frame_start": trims.video.start,
            "frame_count": trims.video.count,
            "rate_numerator": int(rate_numerator),
            "rate_denominator": int(rate_denominator),
            "quantized": (
                Fraction(trims.video.start, 1) / rate != trims.start
                or Fraction(trims.video.count, 1) / rate != trims.duration
            ),
        },
    }
    document["timeline"] = timeline_to_dict(timeline)
    if sync is not None:
        document["sync"] = {
            "audio_seconds": float(sync["audio_seconds"]),
            "video_seconds": float(sync["video_seconds"]),
        }
    return document


__all__ = [
    "MemberTrim",
    "PairTimeline",
    "PairTrims",
    "cut_from_audio",
    "cut_from_video",
    "decimal_fraction",
    "frame_offset",
    "frame_rate",
    "interval_document",
    "origin_trims",
    "round_half_up",
    "timeline_after_parent",
    "timeline_for",
    "timeline_from_dict",
    "timeline_to_dict",
    "trims_from_dict",
    "trims_to_dict",
]
