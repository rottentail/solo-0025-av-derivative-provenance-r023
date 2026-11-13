"""Application orchestration for ingestion, rendering, and audits."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import (
    ApplicationError,
    IntegrityMismatch,
    PairNotFound,
    PlanInvalid,
    RenditionNotCompleted,
)
from .models import (
    IntegrityAudit,
    MediaAsset,
    MediaPair,
    ProvenanceEdge,
    Rendition,
)
from .media import (
    PairTimeline,
    cut_from_audio,
    cut_from_video,
    decimal_fraction,
    inspect_media,
    interval_document,
    origin_trims,
    timeline_after_parent,
    timeline_for,
    timeline_from_dict,
    trims_from_dict,
)
from .renditions import (
    compile_interval,
    compile_member_plan,
    compile_pair_plan,
    compile_plan,
    compile_sync,
    execute_pair,
    execute_plan,
    reproduce_output,
    reproduce_pair,
)
from .serialization import (
    deep_copy,
    digest_json,
    new_identifier,
    sha256_hex,
    utc_now,
)
from .storage import (
    ContentFileRepository,
    JsonStateFile,
    ProvenanceRepository,
)
from .validation import (
    require_object,
    require_string,
    validate_filename,
)


def _contains_trim(raw_operations: Any) -> bool:
    """True when a member request names a shared cut rather than extra work."""

    if not isinstance(raw_operations, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "media_trim"
        for item in raw_operations
    )


class MediaProvenanceApplication:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.content_files = ContentFileRepository(self.data_dir / "content")
        self.state_file = JsonStateFile(self.data_dir / "state.json")
        self.repository = ProvenanceRepository(self.state_file)

    def _asset_bytes(self, asset: MediaAsset) -> bytes:
        return self.content_files.read(asset.content_key)

    def _rendition_view(self, rendition: Rendition) -> dict[str, Any]:
        value = rendition.to_dict()
        if rendition.output_asset_id:
            try:
                value["output_asset"] = self.repository.get_asset(
                    rendition.output_asset_id
                ).to_public_dict()
            except ApplicationError:
                value["output_asset"] = None
        else:
            value["output_asset"] = None
        return value

    def ingest_asset(self, filename: Any, data: bytes) -> dict[str, Any]:
        safe_name = validate_filename(filename)
        descriptor = inspect_media(data)
        digest = sha256_hex(data)
        content_key = self.content_files.key_for_digest(digest)
        existed = self.content_files.exists(content_key)
        self.content_files.write(data, digest)
        asset = MediaAsset(
            id=new_identifier("asset"),
            filename=safe_name,
            kind=descriptor.kind,
            sha256=digest,
            size_bytes=len(data),
            content_key=content_key,
            descriptor=descriptor,
            created_at=utc_now(),
        )
        try:
            persisted = self.repository.create_asset(asset)
        except Exception:
            if not existed:
                self.content_files.remove(content_key)
            raise
        return persisted.to_public_dict()

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        return self.repository.get_asset(asset_id).to_public_dict()

    def _pair_view(self, pair: MediaPair) -> dict[str, Any]:
        audio = self.repository.get_asset(pair.audio_output_asset_id)
        video = self.repository.get_asset(pair.video_output_asset_id)
        value = pair.to_dict()
        value["audio_output_asset"] = audio.to_public_dict()
        value["video_output_asset"] = video.to_public_dict()
        value["audio_rendition"] = self._rendition_view(
            self.repository.get_rendition(pair.audio_rendition_id)
        )
        value["video_rendition"] = self._rendition_view(
            self.repository.get_rendition(pair.video_rendition_id)
        )
        return value

    def _write_output(
        self,
        data: bytes,
        filename: str,
        descriptor: Any,
    ) -> tuple[MediaAsset, str, bool]:
        digest = sha256_hex(data)
        key = self.content_files.key_for_digest(digest)
        existed = self.content_files.exists(key)
        self.content_files.write(data, digest)
        asset = MediaAsset(
            id=new_identifier("asset"),
            filename=filename,
            kind=descriptor.kind,
            sha256=digest,
            size_bytes=len(data),
            content_key=key,
            descriptor=descriptor,
            created_at=utc_now(),
        )
        return asset, key, existed

    @staticmethod
    def _member_rendition(
        *,
        asset_id: str,
        operations: list[dict[str, Any]],
        plan_digest: str,
        output_name: str,
        pair_id: str,
        pair_role: str,
        interval: dict[str, Any],
        now: str,
    ) -> Rendition:
        return Rendition(
            id=new_identifier("rendition"),
            primary_asset_id=asset_id,
            secondary_asset_id=None,
            operations=operations,
            plan_digest=plan_digest,
            output_name=output_name,
            status="queued",
            transitions=[{"from": None, "to": "queued", "at": now}],
            output_asset_id=None,
            error_code=None,
            error_message=None,
            created_at=now,
            updated_at=now,
            pair_id=pair_id,
            pair_role=pair_role,
            interval=deep_copy(interval),
        )

    def create_pair(self, payload: Any) -> dict[str, Any]:
        request = require_object(payload, "request")
        audio_asset_id = require_string(
            request.get("audio_asset_id"),
            "audio_asset_id",
            maximum=96,
        )
        video_asset_id = require_string(
            request.get("video_asset_id"),
            "video_asset_id",
            maximum=96,
        )
        audio_name = validate_filename(request.get("audio_output_name"))
        video_name = validate_filename(request.get("video_output_name"))
        if not audio_name.lower().endswith(".wav"):
            raise PlanInvalid("audio_output_name must end with .wav.")
        if not video_name.lower().endswith(".y4m"):
            raise PlanInvalid("video_output_name must end with .y4m.")

        audio_asset = self.repository.get_asset(audio_asset_id)
        video_asset = self.repository.get_asset(video_asset_id)
        if audio_asset.kind != "audio":
            raise PlanInvalid("audio_asset_id does not reference audio media.")
        if video_asset.kind != "video":
            raise PlanInvalid("video_asset_id does not reference video media.")

        sync = compile_sync(request.get("sync", {}))
        interval = compile_interval(request.get("interval"))
        plan = compile_pair_plan(request.get("operations", {}))

        audio_descriptor = audio_asset.descriptor
        video_descriptor = video_asset.descriptor
        audio_rate = int(audio_descriptor.properties["sample_rate"])
        rate_numerator = int(video_descriptor.properties["rate_numerator"])
        rate_denominator = int(video_descriptor.properties["rate_denominator"])

        trims = origin_trims(
            audio_sync=decimal_fraction(sync["audio_seconds"]),
            video_sync=decimal_fraction(sync["video_seconds"]),
            start=decimal_fraction(interval["start_seconds"]),
            duration=decimal_fraction(interval["duration_seconds"]),
            audio_rate=audio_rate,
            rate_numerator=rate_numerator,
            rate_denominator=rate_denominator,
        )
        self._check_pair_bounds(
            trims,
            audio_frames=int(audio_descriptor.properties["frame_count"]),
            video_frames=int(video_descriptor.properties["frame_count"]),
        )
        timeline = timeline_for(
            trims,
            generation=0,
            audio_rate=audio_rate,
            rate_numerator=rate_numerator,
            rate_denominator=rate_denominator,
        )
        interval_doc = interval_document(
            trims,
            timeline=timeline,
            audio_rate=audio_rate,
            rate_numerator=rate_numerator,
            rate_denominator=rate_denominator,
            sync=sync,
        )
        plan_digest = digest_json(
            {
                "pair": plan.digest,
                "sync": sync,
                "interval": interval,
            }
        )

        pair_id = new_identifier("pair")
        now = utc_now()
        audio_rendition = self._member_rendition(
            asset_id=audio_asset.id,
            operations=plan.audio_operations,
            plan_digest=plan_digest,
            output_name=audio_name,
            pair_id=pair_id,
            pair_role="audio",
            interval=interval_doc,
            now=now,
        )
        video_rendition = self._member_rendition(
            asset_id=video_asset.id,
            operations=plan.video_operations,
            plan_digest=plan_digest,
            output_name=video_name,
            pair_id=pair_id,
            pair_role="video",
            interval=interval_doc,
            now=now,
        )
        return self._execute_and_commit_pair(
            pair_id=pair_id,
            now=now,
            audio_parent=audio_asset,
            video_parent=video_asset,
            audio_rendition=audio_rendition,
            video_rendition=video_rendition,
            audio_operations=list(plan.audio_operations),
            video_operations=list(plan.video_operations),
            trims=trims,
            interval_doc=interval_doc,
            plan_digest=plan_digest,
            parent_pair_id=None,
        )

    def _execute_and_commit_pair(
        self,
        *,
        pair_id: str,
        now: str,
        audio_parent: MediaAsset,
        video_parent: MediaAsset,
        audio_rendition: Rendition,
        video_rendition: Rendition,
        audio_operations: list[dict[str, Any]],
        video_operations: list[dict[str, Any]],
        trims: Any,
        interval_doc: dict[str, Any],
        plan_digest: str,
        parent_pair_id: str | None,
    ) -> dict[str, Any]:
        """Render both members and commit them and the pair atomically.

        Any failure marks both members failed and removes newly written blobs,
        so a delivery can never expose one successful medium.
        """

        self.repository.create_renditions([audio_rendition, video_rendition])
        self.repository.transition_renditions(
            [audio_rendition.id, video_rendition.id],
            "running",
        )

        audio_key: str | None = None
        video_key: str | None = None
        audio_existed = video_existed = False
        try:
            outcome = execute_pair(
                audio_bytes=self._asset_bytes(audio_parent),
                video_bytes=self._asset_bytes(video_parent),
                trims=trims,
                audio_operations=audio_operations,
                video_operations=video_operations,
            )
            audio_output, audio_key, audio_existed = self._write_output(
                outcome.audio_data,
                audio_rendition.output_name,
                outcome.audio_descriptor,
            )
            video_output, video_key, video_existed = self._write_output(
                outcome.video_data,
                video_rendition.output_name,
                outcome.video_descriptor,
            )
            audio_edge = ProvenanceEdge(
                id=new_identifier("edge"),
                rendition_id=audio_rendition.id,
                parent_asset_ids=[audio_parent.id],
                operation_types=[
                    "media_trim",
                    *(item["type"] for item in audio_operations),
                ],
                plan_digest=plan_digest,
                output_asset_id=audio_output.id,
                created_at=utc_now(),
            )
            video_edge = ProvenanceEdge(
                id=new_identifier("edge"),
                rendition_id=video_rendition.id,
                parent_asset_ids=[video_parent.id],
                operation_types=[
                    "media_trim",
                    *(item["type"] for item in video_operations),
                ],
                plan_digest=plan_digest,
                output_asset_id=video_output.id,
                created_at=utc_now(),
            )
            pair = MediaPair(
                id=pair_id,
                audio_rendition_id=audio_rendition.id,
                video_rendition_id=video_rendition.id,
                audio_output_asset_id=audio_output.id,
                video_output_asset_id=video_output.id,
                audio_parent_asset_id=audio_parent.id,
                video_parent_asset_id=video_parent.id,
                interval=interval_doc,
                audio_operations=deep_copy(audio_operations),
                video_operations=deep_copy(video_operations),
                plan_digest=plan_digest,
                parent_pair_id=parent_pair_id,
                status="completed",
                created_at=now,
                updated_at=utc_now(),
            )
            try:
                self.repository.create_pair(
                    pair,
                    [audio_rendition, video_rendition],
                    [audio_output, video_output],
                    [audio_edge, video_edge],
                )
            except Exception:
                for key, existed in (
                    (audio_key, audio_existed),
                    (video_key, video_existed),
                ):
                    if key and not existed:
                        self.content_files.remove(key)
                raise
            return self._pair_view(self.repository.get_pair(pair_id))
        except ApplicationError as exc:
            self.repository.fail_renditions(
                [audio_rendition.id, video_rendition.id],
                exc.code,
                exc.message,
            )
            raise
        except Exception as exc:
            self.repository.fail_renditions(
                [audio_rendition.id, video_rendition.id],
                "RENDER_FAILED",
                "The media pair failed before completion.",
            )
            raise ApplicationError(
                code="RENDER_FAILED",
                message="The media pair failed before completion.",
                status=409,
            ) from exc

    @staticmethod
    def _check_pair_bounds(
        trims: Any,
        *,
        audio_frames: int,
        video_frames: int,
    ) -> None:
        """Reject intervals that do not fit entirely inside both sources.

        Both members must cover the same requested range.  Truncating one
        member at a source edge would silently shorten only that medium, which
        is exactly the desynchronization the pair must prevent.
        """

        if trims.audio.start < 0 or trims.audio.start + trims.audio.count > audio_frames:
            raise PlanInvalid(
                "The shared interval does not fit inside the audio source.",
                details={
                    "audio_sample_start": trims.audio.start,
                    "audio_sample_count": trims.audio.count,
                    "audio_frame_count": audio_frames,
                },
            )
        if trims.video.start < 0 or trims.video.start + trims.video.count > video_frames:
            raise PlanInvalid(
                "The shared interval does not fit inside the video source.",
                details={
                    "video_frame_start": trims.video.start,
                    "video_frame_count": trims.video.count,
                    "video_available_frames": video_frames,
                },
            )

    def get_pair(self, pair_id: str) -> dict[str, Any]:
        return self._pair_view(self.repository.get_pair(pair_id))

    def get_rendition_pair(self, rendition_id: str) -> dict[str, Any]:
        rendition = self.repository.get_rendition(rendition_id)
        if not rendition.pair_id:
            raise PairNotFound(rendition_id)
        return self._pair_view(self.repository.get_pair(rendition.pair_id))

    def create_rendition(self, payload: Any) -> dict[str, Any]:
        request = require_object(payload, "request")
        primary_asset_id = require_string(
            request.get("primary_asset_id"),
            "primary_asset_id",
            maximum=96,
        )
        secondary_value = request.get("secondary_asset_id")
        secondary_asset_id = None
        if secondary_value is not None:
            secondary_asset_id = require_string(
                secondary_value,
                "secondary_asset_id",
                maximum=96,
            )
        output_name = validate_filename(request.get("output_name"))

        primary = self.repository.get_asset(primary_asset_id)
        secondary = (
            self.repository.get_asset(secondary_asset_id)
            if secondary_asset_id
            else None
        )
        if secondary is not None and secondary.kind != primary.kind:
            raise PlanInvalid("Primary and secondary assets must share one media kind.")

        parent_pair = self.repository.pair_for_output_asset(primary.id)
        raw_operations = request.get("operations")
        if parent_pair is not None and _contains_trim(raw_operations):
            if secondary is not None:
                raise PlanInvalid(
                    "A pair member derivation does not accept a secondary asset."
                )
            member_plan = compile_member_plan(
                primary.kind,
                raw_operations,
            )
            return self._create_member_rendition(
                parent_pair=parent_pair,
                member_asset=primary,
                member_plan=member_plan,
                output_name=output_name,
                request=request,
            )

        plan = compile_plan(
            primary.kind,
            request.get("operations"),
            has_secondary=secondary is not None,
        )
        if secondary is not None and "audio_mix" not in plan.operation_types:
            raise PlanInvalid(
                "A secondary audio asset requires an audio_mix operation."
            )
        if primary.kind == "audio" and not output_name.lower().endswith(".wav"):
            raise PlanInvalid("Audio output_name must end with .wav.")
        if primary.kind == "video" and not output_name.lower().endswith(".y4m"):
            raise PlanInvalid("Video output_name must end with .y4m.")

        now = utc_now()
        rendition = Rendition(
            id=new_identifier("rendition"),
            primary_asset_id=primary.id,
            secondary_asset_id=secondary.id if secondary else None,
            operations=plan.operations,
            plan_digest=plan.digest,
            output_name=output_name,
            status="queued",
            transitions=[{"from": None, "to": "queued", "at": now}],
            output_asset_id=None,
            error_code=None,
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        self.repository.create_rendition(rendition)
        self.repository.transition_rendition(rendition.id, "running")

        try:
            primary_bytes = self._asset_bytes(primary)
            secondary_bytes = self._asset_bytes(secondary) if secondary else None
            outcome = execute_plan(
                primary_bytes,
                plan.operations,
                secondary_bytes,
            )
            output_digest = sha256_hex(outcome.data)
            output_key = self.content_files.key_for_digest(output_digest)
            output_existed = self.content_files.exists(output_key)
            self.content_files.write(outcome.data, output_digest)
            output_asset = MediaAsset(
                id=new_identifier("asset"),
                filename=output_name,
                kind=outcome.descriptor.kind,
                sha256=output_digest,
                size_bytes=len(outcome.data),
                content_key=output_key,
                descriptor=outcome.descriptor,
                created_at=utc_now(),
            )
            edge = ProvenanceEdge(
                id=new_identifier("edge"),
                rendition_id=rendition.id,
                parent_asset_ids=[
                    item
                    for item in (primary.id, secondary.id if secondary else None)
                    if item is not None
                ],
                operation_types=list(outcome.operation_types),
                plan_digest=plan.digest,
                output_asset_id=output_asset.id,
                created_at=utc_now(),
            )
            try:
                completed = self.repository.complete_rendition(
                    rendition.id,
                    output_asset,
                    edge,
                )
            except Exception:
                if not output_existed:
                    self.content_files.remove(output_key)
                raise
            return self._rendition_view(completed)
        except ApplicationError as exc:
            current = self.repository.get_rendition(rendition.id)
            if current.status == "running":
                self.repository.fail_rendition(
                    rendition.id,
                    exc.code,
                    exc.message,
                )
            raise
        except Exception as exc:
            current = self.repository.get_rendition(rendition.id)
            if current.status == "running":
                self.repository.fail_rendition(
                    rendition.id,
                    "RENDER_FAILED",
                    "The rendition failed before completion.",
                )
            raise ApplicationError(
                code="RENDER_FAILED",
                message="The rendition failed before completion.",
                status=409,
            ) from exc

    def _create_member_rendition(
        self,
        *,
        parent_pair: MediaPair,
        member_asset: MediaAsset,
        member_plan: Any,
        output_name: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        role = self.repository.pair_role_for_output_asset(member_asset.id)
        if role is None:
            raise PairNotFound(parent_pair.id)
        other_role = "video" if role == "audio" else "audio"
        if role == "audio" and not output_name.lower().endswith(".wav"):
            raise PlanInvalid("Audio output_name must end with .wav.")
        if role == "video" and not output_name.lower().endswith(".y4m"):
            raise PlanInvalid("Video output_name must end with .y4m.")

        counterpart_name_value = request.get("counterpart_output_name")
        counterpart_name = (
            validate_filename(counterpart_name_value)
            if counterpart_name_value is not None
            else None
        )
        if role == "audio" and counterpart_name is not None:
            if not counterpart_name.lower().endswith(".y4m"):
                raise PlanInvalid("counterpart_output_name must end with .y4m.")
        if role == "video" and counterpart_name is not None:
            if not counterpart_name.lower().endswith(".wav"):
                raise PlanInvalid("counterpart_output_name must end with .wav.")
        if counterpart_name is None:
            counterpart_name = self._counterpart_name(output_name, other_role)

        parent_audio = self.repository.get_asset(parent_pair.audio_output_asset_id)
        parent_video = self.repository.get_asset(parent_pair.video_output_asset_id)
        audio_rate = int(parent_audio.descriptor.properties["sample_rate"])
        rate_numerator = int(parent_video.descriptor.properties["rate_numerator"])
        rate_denominator = int(parent_video.descriptor.properties["rate_denominator"])
        parent_timeline = self._pair_timeline(parent_pair, audio_rate, rate_numerator, rate_denominator)

        if role == "audio":
            trims = cut_from_audio(
                parent_timeline,
                offset=decimal_fraction(member_plan.trim["start_seconds"]),
                duration=decimal_fraction(member_plan.trim["duration_seconds"]),
                audio_rate=audio_rate,
                rate_numerator=rate_numerator,
                rate_denominator=rate_denominator,
            )
        else:
            trims = cut_from_video(
                parent_timeline,
                start_frame=int(member_plan.trim["start_frame"]),
                frame_count=int(member_plan.trim["frame_count"]),
                audio_rate=audio_rate,
                rate_numerator=rate_numerator,
                rate_denominator=rate_denominator,
            )
        self._check_pair_bounds(
            trims,
            audio_frames=int(parent_audio.descriptor.properties["frame_count"]),
            video_frames=int(parent_video.descriptor.properties["frame_count"]),
        )
        timeline = timeline_after_parent(
            parent_timeline,
            trims,
            audio_rate=audio_rate,
            rate_numerator=rate_numerator,
            rate_denominator=rate_denominator,
        )
        interval_doc = interval_document(
            trims,
            timeline=timeline,
            audio_rate=audio_rate,
            rate_numerator=rate_numerator,
            rate_denominator=rate_denominator,
            sync=self._pair_sync(parent_pair),
        )

        # Only the requesting member gains extra operations; the counterpart
        # receives the same shared cut with no extra work.
        audio_operations = (
            deep_copy(member_plan.remaining_operations) if role == "audio" else []
        )
        video_operations = (
            deep_copy(member_plan.remaining_operations) if role == "video" else []
        )
        plan_digest = digest_json(
            {
                "parent_pair_id": parent_pair.id,
                "trim": member_plan.trim,
                "audio": audio_operations,
                "video": video_operations,
            }
        )

        pair_id = new_identifier("pair")
        now = utc_now()
        audio_rendition = self._member_rendition(
            asset_id=parent_audio.id,
            operations=audio_operations,
            plan_digest=plan_digest,
            output_name=output_name if role == "audio" else counterpart_name,
            pair_id=pair_id,
            pair_role="audio",
            interval=interval_doc,
            now=now,
        )
        video_rendition = self._member_rendition(
            asset_id=parent_video.id,
            operations=video_operations,
            plan_digest=plan_digest,
            output_name=counterpart_name if role == "audio" else output_name,
            pair_id=pair_id,
            pair_role="video",
            interval=interval_doc,
            now=now,
        )
        return self._execute_and_commit_pair(
            pair_id=pair_id,
            now=now,
            audio_parent=parent_audio,
            video_parent=parent_video,
            audio_rendition=audio_rendition,
            video_rendition=video_rendition,
            audio_operations=audio_operations,
            video_operations=video_operations,
            trims=trims,
            interval_doc=interval_doc,
            plan_digest=plan_digest,
            parent_pair_id=parent_pair.id,
        )

    @staticmethod
    def _counterpart_name(output_name: str, other_role: str) -> str:
        stem = output_name.rsplit(".", 1)[0]
        suffix = ".wav" if other_role == "audio" else ".y4m"
        return f"{stem}-{other_role}{suffix}"

    def _pair_timeline(
        self,
        pair: MediaPair,
        audio_rate: int,
        rate_numerator: int,
        rate_denominator: int,
    ) -> PairTimeline:
        """Restore the absolute original-source timeline of an existing pair."""

        recorded = pair.interval.get("timeline")
        if isinstance(recorded, dict):
            return timeline_from_dict(recorded)
        return timeline_for(
            self._pair_trims(pair),
            generation=0,
            audio_rate=audio_rate,
            rate_numerator=rate_numerator,
            rate_denominator=rate_denominator,
        )

    @staticmethod
    def _pair_trims(pair: MediaPair) -> Any:
        return trims_from_dict(pair.interval)

    @staticmethod
    def _pair_sync(pair: MediaPair) -> dict[str, float] | None:
        sync = pair.interval.get("sync")
        return dict(sync) if isinstance(sync, dict) else None

    def get_rendition(self, rendition_id: str) -> dict[str, Any]:
        return self._rendition_view(self.repository.get_rendition(rendition_id))

    def get_lineage(self, rendition_id: str) -> dict[str, Any]:
        return self.repository.lineage_for(rendition_id)

    def _begin_integrity_audit(self, rendition_id: str) -> IntegrityAudit:
        now = utc_now()
        audit = IntegrityAudit(
            id=new_identifier("audit"),
            rendition_id=rendition_id,
            status="pending",
            expected_digest=None,
            reproduced_digest=None,
            expected_descriptor=None,
            reproduced_descriptor=None,
            reason=None,
            created_at=now,
            updated_at=now,
        )
        return self.repository.insert_integrity_audit(audit)

    def _finish_mismatch(
        self,
        audit: IntegrityAudit,
        reason: str,
        *,
        expected_digest: str | None = None,
        expected_descriptor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        finished = self.repository.settle_integrity_audit(
            audit.id,
            status="mismatch",
            expected_digest=expected_digest,
            reproduced_digest=None,
            expected_descriptor=expected_descriptor,
            reproduced_descriptor=None,
            reason=reason,
        )
        body = finished.to_dict()
        raise IntegrityMismatch(
            "The recorded rendition did not reproduce exactly.",
            details={
                "audit": body,
                "reason": reason,
            },
        )

    def verify_rendition(self, rendition_id: str) -> dict[str, Any]:
        rendition = self.repository.get_rendition(rendition_id)
        if rendition.status != "completed" or not rendition.output_asset_id:
            raise RenditionNotCompleted(rendition_id, rendition.status)
        if rendition.pair_id:
            return self._verify_pair_member(rendition, rendition.pair_id)

        audit = self._begin_integrity_audit(rendition_id)
        try:
            edge = self.repository.get_edge(rendition_id)
            output_asset = self.repository.get_asset(rendition.output_asset_id)
            primary_asset = self.repository.get_asset(rendition.primary_asset_id)
            secondary_asset = (
                self.repository.get_asset(rendition.secondary_asset_id)
                if rendition.secondary_asset_id
                else None
            )
        except ApplicationError:
            self._finish_mismatch(audit, "provenance_unavailable")

        expected_descriptor = output_asset.descriptor.to_dict()
        try:
            recorded_output = self._asset_bytes(output_asset)
            primary_bytes = self._asset_bytes(primary_asset)
            secondary_bytes = (
                self._asset_bytes(secondary_asset) if secondary_asset else None
            )
        except ApplicationError:
            self._finish_mismatch(
                audit,
                "source_or_output_unavailable",
                expected_digest=output_asset.sha256,
                expected_descriptor=expected_descriptor,
            )

        if edge.plan_digest != rendition.plan_digest:
            self._finish_mismatch(
                audit,
                "plan_digest_drift",
                expected_digest=output_asset.sha256,
                expected_descriptor=expected_descriptor,
            )
        if edge.parent_asset_ids != [
            item
            for item in (
                rendition.primary_asset_id,
                rendition.secondary_asset_id,
            )
            if item is not None
        ]:
            self._finish_mismatch(
                audit,
                "parent_set_drift",
                expected_digest=output_asset.sha256,
                expected_descriptor=expected_descriptor,
            )

        outcome = reproduce_output(
            output_asset=output_asset,
            recorded_output_bytes=recorded_output,
            primary_bytes=primary_bytes,
            operations=deep_copy(rendition.operations),
            secondary_bytes=secondary_bytes,
        )
        finished = self.repository.settle_integrity_audit(
            audit.id,
            status=outcome.status,
            expected_digest=outcome.expected_digest,
            reproduced_digest=outcome.reproduced_digest,
            expected_descriptor=outcome.expected_descriptor,
            reproduced_descriptor=outcome.reproduced_descriptor,
            reason=outcome.reason,
        )
        if finished.status == "verified":
            return finished.to_dict()
        raise IntegrityMismatch(
            "The recorded rendition did not reproduce exactly.",
            details={
                "audit": finished.to_dict(),
                "reason": finished.reason,
            },
        )

    def _verify_pair_member(
        self,
        rendition: Rendition,
        pair_id: str,
    ) -> dict[str, Any]:
        """Verify a pair member by replaying the whole pair, never one medium."""

        audit = self._begin_integrity_audit(rendition.id)
        try:
            pair = self.repository.get_pair(pair_id)
        except ApplicationError:
            self._finish_mismatch(audit, "provenance_unavailable")

        audio_rendition = self.repository.get_rendition(pair.audio_rendition_id)
        video_rendition = self.repository.get_rendition(pair.video_rendition_id)
        if (
            audio_rendition.status != "completed"
            or video_rendition.status != "completed"
            or not audio_rendition.output_asset_id
            or not video_rendition.output_asset_id
        ):
            self._finish_mismatch(audit, "pair_incomplete")
        if (
            audio_rendition.output_asset_id != pair.audio_output_asset_id
            or video_rendition.output_asset_id != pair.video_output_asset_id
            or audio_rendition.pair_id != pair.id
            or video_rendition.pair_id != pair.id
        ):
            self._finish_mismatch(audit, "pair_member_drift")

        try:
            audio_edge = self.repository.get_edge(audio_rendition.id)
            video_edge = self.repository.get_edge(video_rendition.id)
        except ApplicationError:
            self._finish_mismatch(audit, "provenance_unavailable")
        if (
            audio_edge.plan_digest != pair.plan_digest
            or video_edge.plan_digest != pair.plan_digest
            or audio_edge.plan_digest != video_rendition.plan_digest
        ):
            self._finish_mismatch(audit, "plan_digest_drift")
        if audio_edge.parent_asset_ids != [pair.audio_parent_asset_id]:
            self._finish_mismatch(audit, "parent_set_drift")
        if video_edge.parent_asset_ids != [pair.video_parent_asset_id]:
            self._finish_mismatch(audit, "parent_set_drift")

        try:
            audio_asset = self.repository.get_asset(pair.audio_output_asset_id)
            video_asset = self.repository.get_asset(pair.video_output_asset_id)
            audio_parent = self.repository.get_asset(pair.audio_parent_asset_id)
            video_parent = self.repository.get_asset(pair.video_parent_asset_id)
            recorded_audio = self._asset_bytes(audio_asset)
            recorded_video = self._asset_bytes(video_asset)
            audio_source = self._asset_bytes(audio_parent)
            video_source = self._asset_bytes(video_parent)
        except ApplicationError:
            self._finish_mismatch(audit, "source_or_output_unavailable")

        if audio_asset.kind != "audio" or video_asset.kind != "video":
            self._finish_mismatch(audit, "pair_member_kind_drift")

        audio_rate = int(audio_parent.descriptor.properties["sample_rate"])
        rate_numerator = int(video_parent.descriptor.properties["rate_numerator"])
        rate_denominator = int(video_parent.descriptor.properties["rate_denominator"])
        try:
            trims = self._pair_trims(pair)
        except (KeyError, TypeError, ValueError):
            self._finish_mismatch(audit, "interval_drift")

        outcome = reproduce_pair(
            audio_asset=audio_asset,
            recorded_audio_bytes=recorded_audio,
            video_asset=video_asset,
            recorded_video_bytes=recorded_video,
            audio_source_bytes=audio_source,
            video_source_bytes=video_source,
            trims=trims,
            audio_operations=deep_copy(pair.audio_operations),
            video_operations=deep_copy(pair.video_operations),
        )
        finished = self.repository.settle_integrity_audit(
            audit.id,
            status=outcome.status,
            expected_digest=outcome.expected_digest,
            reproduced_digest=outcome.reproduced_digest,
            expected_descriptor=outcome.expected_descriptor,
            reproduced_descriptor=outcome.reproduced_descriptor,
            reason=outcome.reason,
            members=outcome.members,
        )
        if finished.status == "verified":
            return finished.to_dict()
        raise IntegrityMismatch(
            "The recorded media pair did not reproduce exactly.",
            details={
                "audit": finished.to_dict(),
                "reason": finished.reason,
                "pair_id": pair.id,
            },
        )

    def close(self) -> None:
        return
