"""Domain repository over the atomic JSON state file."""

from __future__ import annotations

from typing import Any

from ..errors import (
    AssetNotFound,
    InvalidStateTransition,
    PairNotFound,
    RenditionNotFound,
)
from ..models import (
    IntegrityAudit,
    MediaAsset,
    MediaPair,
    ProvenanceEdge,
    Rendition,
)
from ..serialization import deep_copy
from .state_file import JsonStateFile


class ProvenanceRepository:
    def __init__(self, state_file: JsonStateFile) -> None:
        self.state_file = state_file

    def create_asset(self, asset: MediaAsset) -> MediaAsset:
        def mutate(state: dict[str, Any]) -> MediaAsset:
            assets = state["assets"]
            if asset.id in assets:
                raise InvalidStateTransition("asset", "duplicate")
            assets[asset.id] = asset.to_dict()
            return asset

        return self.state_file.mutate(mutate)

    def get_asset(self, asset_id: str) -> MediaAsset:
        state = self.state_file.read()
        raw = state["assets"].get(asset_id)
        if not isinstance(raw, dict):
            raise AssetNotFound(asset_id)
        return MediaAsset.from_dict(deep_copy(raw))

    def create_rendition(self, rendition: Rendition) -> Rendition:
        def mutate(state: dict[str, Any]) -> Rendition:
            renditions = state["renditions"]
            if rendition.id in renditions:
                raise InvalidStateTransition("rendition", "duplicate")
            renditions[rendition.id] = rendition.to_dict()
            return rendition

        return self.state_file.mutate(mutate)

    def get_rendition(self, rendition_id: str) -> Rendition:
        state = self.state_file.read()
        raw = state["renditions"].get(rendition_id)
        if not isinstance(raw, dict):
            raise RenditionNotFound(rendition_id)
        return Rendition.from_dict(deep_copy(raw))

    def transition_rendition(self, rendition_id: str, target: str) -> Rendition:
        def mutate(state: dict[str, Any]) -> Rendition:
            raw = state["renditions"].get(rendition_id)
            if not isinstance(raw, dict):
                raise RenditionNotFound(rendition_id)
            rendition = Rendition.from_dict(deep_copy(raw))
            rendition.transition_to(target)
            state["renditions"][rendition_id] = rendition.to_dict()
            return rendition

        return self.state_file.mutate(mutate)

    def fail_rendition(
        self,
        rendition_id: str,
        error_code: str,
        error_message: str,
    ) -> Rendition:
        def mutate(state: dict[str, Any]) -> Rendition:
            raw = state["renditions"].get(rendition_id)
            if not isinstance(raw, dict):
                raise RenditionNotFound(rendition_id)
            rendition = Rendition.from_dict(deep_copy(raw))
            rendition.error_code = error_code
            rendition.error_message = error_message
            rendition.transition_to("failed")
            state["renditions"][rendition_id] = rendition.to_dict()
            return rendition

        return self.state_file.mutate(mutate)

    def complete_rendition(
        self,
        rendition_id: str,
        output_asset: MediaAsset,
        edge: ProvenanceEdge,
    ) -> Rendition:
        def mutate(state: dict[str, Any]) -> Rendition:
            raw = state["renditions"].get(rendition_id)
            if not isinstance(raw, dict):
                raise RenditionNotFound(rendition_id)
            rendition = Rendition.from_dict(deep_copy(raw))
            if output_asset.id in state["assets"]:
                raise InvalidStateTransition("asset", "duplicate")
            state["assets"][output_asset.id] = output_asset.to_dict()
            state["provenance_edges"].append(edge.to_dict())
            rendition.output_asset_id = output_asset.id
            rendition.error_code = None
            rendition.error_message = None
            rendition.transition_to("completed")
            state["renditions"][rendition_id] = rendition.to_dict()
            return rendition

        return self.state_file.mutate(mutate)

    def create_renditions(self, renditions: list[Rendition]) -> list[Rendition]:
        """Persist several member renditions in one state mutation."""

        def mutate(state: dict[str, Any]) -> list[Rendition]:
            records = state["renditions"]
            for rendition in renditions:
                if rendition.id in records:
                    raise InvalidStateTransition("rendition", "duplicate")
            for rendition in renditions:
                records[rendition.id] = rendition.to_dict()
            return list(renditions)

        return self.state_file.mutate(mutate)

    def transition_renditions(
        self,
        rendition_ids: list[str],
        target: str,
    ) -> list[Rendition]:
        """Advance several member renditions together or not at all."""

        def mutate(state: dict[str, Any]) -> list[Rendition]:
            prepared: list[Rendition] = []
            for rendition_id in rendition_ids:
                raw = state["renditions"].get(rendition_id)
                if not isinstance(raw, dict):
                    raise RenditionNotFound(rendition_id)
                rendition = Rendition.from_dict(deep_copy(raw))
                rendition.transition_to(target)
                prepared.append(rendition)
            for rendition in prepared:
                state["renditions"][rendition.id] = rendition.to_dict()
            return prepared

        return self.state_file.mutate(mutate)

    def fail_renditions(
        self,
        rendition_ids: list[str],
        error_code: str,
        error_message: str,
    ) -> list[Rendition]:
        """Fail every running member of a pair in one state mutation."""

        def mutate(state: dict[str, Any]) -> list[Rendition]:
            prepared: list[Rendition] = []
            for rendition_id in rendition_ids:
                raw = state["renditions"].get(rendition_id)
                if not isinstance(raw, dict):
                    raise RenditionNotFound(rendition_id)
                rendition = Rendition.from_dict(deep_copy(raw))
                if rendition.status != "running":
                    continue
                rendition.error_code = error_code
                rendition.error_message = error_message
                rendition.transition_to("failed")
                prepared.append(rendition)
            for rendition in prepared:
                state["renditions"][rendition.id] = rendition.to_dict()
            return prepared

        return self.state_file.mutate(mutate)

    def create_pair(
        self,
        pair: MediaPair,
        renditions: list[Rendition],
        output_assets: list[MediaAsset],
        edges: list[ProvenanceEdge],
    ) -> MediaPair:
        """Commit a pair, both members, both outputs, and both edges at once.

        Either every record becomes visible or none does; a partially visible
        pair would report success for only one medium.
        """

        if len(renditions) != 2 or len(output_assets) != 2 or len(edges) != 2:
            raise InvalidStateTransition("pair", "incomplete_members")
        expected_roles = {"audio", "video"}
        if {item.pair_role for item in renditions} != expected_roles:
            raise InvalidStateTransition("pair", "role_mismatch")

        def mutate(state: dict[str, Any]) -> MediaPair:
            pairs = state["pairs"]
            if pair.id in pairs:
                raise InvalidStateTransition("pair", "duplicate")
            for asset in output_assets:
                if asset.id in state["assets"]:
                    raise InvalidStateTransition("asset", "duplicate")
            completed_members: list[Rendition] = []
            output_by_kind = {asset.kind: asset for asset in output_assets}
            if set(output_by_kind) != expected_roles:
                raise InvalidStateTransition("pair", "role_mismatch")
            for member in renditions:
                raw = state["renditions"].get(member.id)
                if not isinstance(raw, dict):
                    raise RenditionNotFound(member.id)
                current = Rendition.from_dict(deep_copy(raw))
                if current.status != "running":
                    raise InvalidStateTransition(current.status, "completed")
                if current.pair_id != pair.id or current.pair_role != member.pair_role:
                    raise InvalidStateTransition("rendition", "pair_mismatch")
                current.output_asset_id = output_by_kind[member.pair_role].id
                current.error_code = None
                current.error_message = None
                current.interval = deep_copy(pair.interval)
                current.transition_to("completed")
                completed_members.append(current)
            for asset in output_assets:
                state["assets"][asset.id] = asset.to_dict()
            for edge in edges:
                state["provenance_edges"].append(edge.to_dict())
            for member in completed_members:
                state["renditions"][member.id] = member.to_dict()
            pairs[pair.id] = pair.to_dict()
            return pair

        return self.state_file.mutate(mutate)

    def get_pair(self, pair_id: str) -> MediaPair:
        state = self.state_file.read()
        raw = state["pairs"].get(pair_id)
        if not isinstance(raw, dict):
            raise PairNotFound(pair_id)
        return MediaPair.from_dict(deep_copy(raw))

    def pair_for_output_asset(self, asset_id: str) -> MediaPair | None:
        """The delivery whose member is exactly this output asset.

        Each output asset belongs to one pair, so the lookup is unambiguous.
        Re-cutting that member starts a new child delivery from this pair.
        """

        state = self.state_file.read()
        for raw in state["pairs"].values():
            if not isinstance(raw, dict):
                continue
            if asset_id in (
                raw.get("audio_output_asset_id"),
                raw.get("video_output_asset_id"),
            ):
                return MediaPair.from_dict(deep_copy(raw))
        return None

    def pair_role_for_output_asset(self, asset_id: str) -> str | None:
        pair = self.pair_for_output_asset(asset_id)
        if pair is None:
            return None
        if pair.audio_output_asset_id == asset_id:
            return "audio"
        if pair.video_output_asset_id == asset_id:
            return "video"
        return None

    def get_edge(self, rendition_id: str) -> ProvenanceEdge:
        state = self.state_file.read()
        for raw in state["provenance_edges"]:
            if isinstance(raw, dict) and raw.get("rendition_id") == rendition_id:
                return ProvenanceEdge.from_dict(deep_copy(raw))
        raise RenditionNotFound(rendition_id)

    def lineage_for(self, rendition_id: str) -> dict[str, Any]:
        rendition = self.get_rendition(rendition_id)
        if rendition.status != "completed":
            return {
                "rendition_id": rendition_id,
                "status": rendition.status,
                "parents": [],
                "output": None,
                "edge": None,
            }
        edge = self.get_edge(rendition_id)
        parents = [self.get_asset(asset_id).to_public_dict() for asset_id in edge.parent_asset_ids]
        output = self.get_asset(edge.output_asset_id).to_public_dict()
        return {
            "rendition_id": rendition_id,
            "status": rendition.status,
            "parents": parents,
            "output": output,
            "edge": edge.to_dict(),
        }

    def insert_integrity_audit(self, audit: IntegrityAudit) -> IntegrityAudit:
        def mutate(state: dict[str, Any]) -> IntegrityAudit:
            audits = state["integrity_checks"]
            if audit.id in audits:
                raise InvalidStateTransition("integrity_check", "duplicate")
            audits[audit.id] = audit.to_dict()
            return audit

        return self.state_file.mutate(mutate)

    def settle_integrity_audit(
        self,
        check_id: str,
        *,
        status: str,
        expected_digest: str | None,
        reproduced_digest: str | None,
        expected_descriptor: dict[str, Any] | None,
        reproduced_descriptor: dict[str, Any] | None,
        reason: str | None,
        members: list[dict[str, Any]] | None = None,
    ) -> IntegrityAudit:
        def mutate(state: dict[str, Any]) -> IntegrityAudit:
            raw = state["integrity_checks"].get(check_id)
            if not isinstance(raw, dict):
                raise RenditionNotFound(check_id)
            audit = IntegrityAudit.from_dict(deep_copy(raw))
            audit.finish(
                status=status,
                expected_digest=expected_digest,
                reproduced_digest=reproduced_digest,
                expected_descriptor=expected_descriptor,
                reproduced_descriptor=reproduced_descriptor,
                reason=reason,
                members=members,
            )
            state["integrity_checks"][check_id] = audit.to_dict()
            return audit

        return self.state_file.mutate(mutate)

    def get_integrity_audit(self, check_id: str) -> IntegrityAudit:
        state = self.state_file.read()
        raw = state["integrity_checks"].get(check_id)
        if not isinstance(raw, dict):
            raise RenditionNotFound(check_id)
        return IntegrityAudit.from_dict(deep_copy(raw))
