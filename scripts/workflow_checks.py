#!/usr/bin/env python3
"""Run bounded production workflow checks against an ephemeral HTTP server."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import io
import json
import math
import struct
import sys
import tempfile
import threading
import wave
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from avprovenance.http_api import ProvenanceHTTPServer, build_http_server


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_wav(
    *,
    frequency: float,
    duration_seconds: float = 0.6,
    sample_rate: int = 8000,
    amplitude: int = 9000,
) -> bytes:
    frame_count = int(duration_seconds * sample_rate)
    values = [
        int(amplitude * math.sin(2 * math.pi * frequency * index / sample_rate))
        for index in range(frame_count)
    ]
    payload = io.BytesIO()
    with wave.open(payload, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(struct.pack(f"<{len(values)}h", *values))
    return payload.getvalue()


def make_y4m(
    *,
    width: int = 64,
    height: int = 36,
    frame_count: int = 5,
    rate_numerator: int = 12,
    rate_denominator: int = 1,
) -> bytes:
    header = (
        "YUV4MPEG2 "
        f"W{width} H{height} "
        f"F{rate_numerator}:{rate_denominator} "
        "Ip C420jpeg\n"
    ).encode("ascii")
    chroma_width = width // 2
    chroma_height = height // 2
    parts = [header]
    for frame_index in range(frame_count):
        parts.append(b"FRAME\n")
        y_plane = bytes(
            (x * 3 + y * 5 + frame_index * 11) % 256
            for y in range(height)
            for x in range(width)
        )
        u_plane = bytes(
            (96 + x * 2 + frame_index * 3) % 256
            for _y in range(chroma_height)
            for x in range(chroma_width)
        )
        v_plane = bytes(
            (144 + y * 2 + frame_index * 5) % 256
            for y in range(chroma_height)
            for _x in range(chroma_width)
        )
        parts.extend((y_plane, u_plane, v_plane))
    return b"".join(parts)


class HTTPClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        connection = http.client.HTTPConnection(
            self.host,
            self.port,
            timeout=15,
        )
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            value = json.loads(raw.decode("utf-8"))
            return response.status, value
        finally:
            connection.close()

    def upload(self, filename: str, data: bytes) -> dict[str, Any]:
        status, body = self.request(
            "POST",
            "/v1/assets",
            {
                "filename": filename,
                "data_base64": base64.b64encode(data).decode("ascii"),
            },
        )
        require_equal(status, 201, "asset upload status")
        return body

    def create_rendition(self, payload: dict[str, Any]) -> dict[str, Any]:
        status, body = self.request("POST", "/v1/renditions", payload)
        require_equal(status, 201, "rendition creation status")
        return body


@contextmanager
def running_server() -> Iterator[tuple[HTTPClient, ProvenanceHTTPServer]]:
    with tempfile.TemporaryDirectory(prefix="avprovenance-check-") as temporary:
        server = build_http_server("127.0.0.1", 0, Path(temporary))
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        thread.start()
        host, port = server.server_address[:2]
        try:
            yield HTTPClient(str(host), int(port)), server
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


def require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_ingest_media_asset(client: HTTPClient) -> dict[str, Any]:
    health_status, health = client.request("GET", "/health")
    require_equal(health_status, 200, "health status")
    require_equal(health["status"], "ok", "health payload")

    audio = make_wav(frequency=330)
    asset = client.upload("tone-source.wav", audio)
    require_equal(asset["kind"], "audio", "asset kind")
    require_equal(asset["sha256"], _sha256(audio), "asset digest")
    require_equal(asset["descriptor"]["container"], "wav", "asset container")
    require("content_key" not in asset, "public asset exposed an internal content key")

    get_status, fetched = client.request("GET", f"/v1/assets/{asset['id']}")
    require_equal(get_status, 200, "asset get status")
    require_equal(fetched["sha256"], asset["sha256"], "asset read digest")

    bad_status, bad_body = client.request(
        "POST",
        "/v1/assets",
        {
            "filename": "unknown.bin",
            "data_base64": base64.b64encode(b"not-media").decode("ascii"),
        },
    )
    require_equal(bad_status, 415, "unsupported media status")
    require_equal(
        bad_body["error"]["code"],
        "UNSUPPORTED_MEDIA",
        "unsupported media code",
    )
    return {
        "workflow": "ingest-media-asset",
        "asset_id": asset["id"],
        "kind": asset["kind"],
        "digest": asset["sha256"],
    }


def run_execute_rendition(client: HTTPClient) -> dict[str, Any]:
    primary_bytes = make_wav(frequency=440, amplitude=10000)
    secondary_bytes = make_wav(frequency=220, amplitude=7000)
    primary = client.upload("primary-tone.wav", primary_bytes)
    secondary = client.upload("secondary-tone.wav", secondary_bytes)

    audio_rendition = client.create_rendition(
        {
            "primary_asset_id": primary["id"],
            "secondary_asset_id": secondary["id"],
            "output_name": "mixed-tone.wav",
            "operations": [
                {
                    "type": "media_trim",
                    "start_seconds": 0.05,
                    "duration_seconds": 0.3,
                },
                {"type": "audio_gain", "db": -3},
                {
                    "type": "audio_mix",
                    "secondary_gain": -6,
                    "primary_weight": 0.75,
                    "secondary_weight": 0.4,
                },
                {"type": "audio_normalize", "target_peak": 0.8},
            ],
        }
    )
    require_equal(audio_rendition["status"], "completed", "audio rendition status")
    history = [item["to"] for item in audio_rendition["transitions"]]
    require_equal(
        history,
        ["queued", "running", "completed"],
        "audio state history",
    )
    require(
        audio_rendition["output_asset"]["descriptor"]["container"] == "wav",
        "audio output descriptor was not WAV",
    )

    video_bytes = make_y4m()
    video = client.upload("source-frames.y4m", video_bytes)
    video_rendition = client.create_rendition(
        {
            "primary_asset_id": video["id"],
            "output_name": "cropped-frames.y4m",
            "operations": [
                {
                    "type": "video_crop",
                    "x": 8,
                    "y": 6,
                    "width": 32,
                    "height": 18,
                },
                {"type": "video_luma_offset", "amount": 12},
                {"type": "video_mirror"},
            ],
        }
    )
    require_equal(video_rendition["status"], "completed", "video rendition status")
    properties = video_rendition["output_asset"]["descriptor"]["properties"]
    require_equal(properties["width"], 32, "video output width")
    require_equal(properties["height"], 18, "video output height")

    lineage_status, lineage = client.request(
        "GET",
        f"/v1/renditions/{audio_rendition['id']}/lineage",
    )
    require_equal(lineage_status, 200, "lineage status")
    require_equal(len(lineage["parents"]), 2, "lineage parent count")
    require_equal(
        lineage["edge"]["plan_digest"],
        audio_rendition["plan_digest"],
        "lineage plan digest",
    )
    invalid_status, invalid_body = client.request(
        "POST",
        "/v1/renditions",
        {
            "primary_asset_id": primary["id"],
            "output_name": "invalid.wav",
            "operations": [
                {
                    "type": "video_crop",
                    "x": 0,
                    "y": 0,
                    "width": 2,
                    "height": 2,
                }
            ],
        },
    )
    require_equal(invalid_status, 422, "invalid plan status")
    require_equal(invalid_body["error"]["code"], "PLAN_INVALID", "invalid plan code")
    return {
        "workflow": "execute-rendition",
        "audio_rendition_id": audio_rendition["id"],
        "video_rendition_id": video_rendition["id"],
        "audio_history": history,
        "invalid_plan_code": invalid_body["error"]["code"],
        "video_dimensions": [
            properties["width"],
            properties["height"],
        ],
    }


def run_verify_reproduction(
    client: HTTPClient,
    server: ProvenanceHTTPServer,
) -> dict[str, Any]:
    video_bytes = make_y4m(frame_count=7)
    video = client.upload("verification-source.y4m", video_bytes)
    rendition = client.create_rendition(
        {
            "primary_asset_id": video["id"],
            "output_name": "verification-output.y4m",
            "operations": [
                {"type": "media_trim", "start_frame": 1, "frame_count": 4},
                {
                    "type": "video_crop",
                    "x": 4,
                    "y": 4,
                    "width": 40,
                    "height": 20,
                },
                {"type": "video_luma_offset", "amount": -9},
                {"type": "video_mirror"},
            ],
        }
    )
    check_status, check = client.request(
        "POST",
        f"/v1/renditions/{rendition['id']}/integrity-checks",
        {},
    )
    require_equal(check_status, 201, "integrity check status")
    require_equal(check["status"], "verified", "integrity result")
    require_equal(
        check["expected_digest"],
        check["reproduced_digest"],
        "integrity digest equality",
    )
    require_equal(
        check["expected_descriptor"],
        check["reproduced_descriptor"],
        "integrity descriptor equality",
    )

    lineage_status, lineage = client.request(
        "GET",
        f"/v1/renditions/{rendition['id']}/lineage",
    )
    require_equal(lineage_status, 200, "verification lineage status")
    require_equal(len(lineage["parents"]), 1, "verification parent count")

    output_asset_id = rendition["output_asset"]["id"]

    def drift_metadata(state: dict[str, Any]) -> None:
        state["assets"][output_asset_id]["sha256"] = "0" * 64

    server.application.state_file.mutate(drift_metadata)
    mismatch_status, mismatch = client.request(
        "POST",
        f"/v1/renditions/{rendition['id']}/integrity-checks",
        {},
    )
    require_equal(mismatch_status, 409, "mismatch status")
    require_equal(
        mismatch["error"]["code"],
        "INTEGRITY_MISMATCH",
        "mismatch error code",
    )
    require_equal(
        mismatch["error"]["details"]["audit"]["status"],
        "mismatch",
        "persisted mismatch status",
    )
    return {
        "workflow": "verify-reproduction",
        "rendition_id": rendition["id"],
        "integrity_check_id": check["id"],
        "digest": check["reproduced_digest"],
        "mismatch_status": mismatch["error"]["details"]["audit"]["status"],
    }


def run_pair_derivation(
    client: HTTPClient,
    server: ProvenanceHTTPServer,
) -> dict[str, Any]:
    from fractions import Fraction

    # A fractional frame rate makes any accumulated rounding visible.
    audio_bytes = make_wav(
        frequency=440,
        duration_seconds=9.0,
        sample_rate=8000,
    )
    video_bytes = make_y4m(
        frame_count=240,
        rate_numerator=240,
        rate_denominator=7,
    )
    audio_asset = client.upload("pair-source.wav", audio_bytes)
    video_asset = client.upload("pair-source.y4m", video_bytes)

    pair_status, pair = client.request(
        "POST",
        "/v1/pairs",
        {
            "audio_asset_id": audio_asset["id"],
            "video_asset_id": video_asset["id"],
            "audio_output_name": "pair-output.wav",
            "video_output_name": "pair-output.y4m",
            "sync": {"audio_seconds": 0.0, "video_seconds": 0.0},
            "interval": {"start_seconds": 0.001, "duration_seconds": 0.5},
            "operations": {
                "audio": [{"type": "audio_gain", "db": -3}],
                "video": [{"type": "video_luma_offset", "amount": 7}],
            },
        },
    )
    require_equal(pair_status, 201, "pair creation status")
    require_equal(pair["status"], "completed", "pair status")
    require(
        pair["audio_output_asset"]["kind"] == "audio"
        and pair["video_output_asset"]["kind"] == "video",
        "pair did not expose both media kinds",
    )
    for role in ("audio", "video"):
        rendition = pair[f"{role}_rendition"]
        require_equal(
            [item["to"] for item in rendition["transitions"]],
            ["queued", "running", "completed"],
            f"{role} rendition history",
        )
        require_equal(rendition["pair_role"], role, f"{role} pair role")
        require_equal(rendition["pair_id"], pair["id"], f"{role} pair id")

    interval = pair["interval"]
    for role in ("audio", "video"):
        require_equal(
            interval["members"][role]["start_seconds"],
            interval["start_seconds"],
            f"{role} member start matches shared start",
        )
        require_equal(
            interval["members"][role]["duration_seconds"],
            interval["duration_seconds"],
            f"{role} member duration matches shared duration",
        )
    require_equal(
        interval["timeline"]["exact"]["audio_requested"],
        interval["timeline"]["exact"]["video_requested"],
        "members share one requested position",
    )

    pair_read_status, fetched_pair = client.request("GET", f"/v1/pairs/{pair['id']}")
    require_equal(pair_read_status, 200, "pair read status")
    require_equal(fetched_pair["interval"], interval, "pair read interval")
    member_read_status, pair_of_member = client.request(
        "GET",
        f"/v1/renditions/{pair['video_rendition_id']}/pair",
    )
    require_equal(member_read_status, 200, "rendition pair status")
    require_equal(pair_of_member["id"], pair["id"], "rendition pair identity")

    check_status, check = client.request(
        "POST",
        f"/v1/renditions/{pair['audio_rendition_id']}/integrity-checks",
        {},
    )
    require_equal(check_status, 201, "pair integrity status")
    require_equal(check["status"], "verified", "pair integrity result")
    require_equal(len(check["members"]), 2, "pair integrity member count")
    require(
        all(item["status"] == "verified" for item in check["members"]),
        "a pair member failed verification",
    )

    # Cutting through one member must move the counterpart too.
    child_status, child = client.request(
        "POST",
        "/v1/renditions",
        {
            "primary_asset_id": pair["audio_output_asset"]["id"],
            "output_name": "pair-child.wav",
            "operations": [
                {"type": "media_trim", "start_seconds": 0.05, "duration_seconds": 0.4},
                {"type": "audio_gain", "db": -2},
            ],
        },
    )
    require_equal(child_status, 201, "child pair status")
    require_equal(child["parent_pair_id"], pair["id"], "child parent pair")
    child_video_frames = child["video_output_asset"]["descriptor"]["properties"][
        "frame_count"
    ]
    require(
        child_video_frames < 96,
        f"counterpart video did not advance: {child_video_frames} frames",
    )
    require_equal(
        child["video_rendition"]["operations"],
        [],
        "counterpart member inherited the requesting member's operations",
    )

    # Each child keeps only the requested sub-interval, so repeated cuts use
    # a shrinking window: offset + duration must stay inside the parent.
    current = child
    generations = [current]
    durations = [0.2, 0.1, 0.05, 0.02, 0.01]
    for index, duration in enumerate(durations):
        status, nxt = client.request(
            "POST",
            "/v1/renditions",
            {
                "primary_asset_id": current["audio_output_asset"]["id"],
                "output_name": f"pair-step-{index}.wav",
                "operations": [
                    {"type": "media_trim", "start_seconds": 0.004, "duration_seconds": duration}
                ],
            },
        )
        require_equal(status, 201, f"repeated cut {index} status")
        current = nxt
        generations.append(current)

    final = generations[-1]["interval"]
    timeline = final["timeline"]
    audio_drift = Fraction(final["exact"]["audio_requested_seconds"]) - Fraction(
        timeline["exact"]["audio_delivered"]
    )
    video_drift = Fraction(final["exact"]["video_requested_seconds"]) - Fraction(
        timeline["exact"]["video_delivered"]
    )
    require(
        abs(audio_drift) <= Fraction(1, 2 * 8000),
        f"audio drift exceeded half a sample: {float(audio_drift)}",
    )
    require(
        abs(video_drift) <= Fraction(7, 240),
        f"video drift exceeded one frame: {float(video_drift)}",
    )
    relative_drift = abs(audio_drift - video_drift)
    require(
        relative_drift <= Fraction(7, 240),
        f"relative drift exceeded one frame: {float(relative_drift)}",
    )
    # The bounded drift must not grow with the number of cuts: the first and
    # last deliveries are held to the same limit.
    first = generations[0]["interval"]["timeline"]
    first_audio_drift = Fraction(
        generations[0]["interval"]["exact"]["audio_requested_seconds"]
    ) - Fraction(first["exact"]["audio_delivered"])
    first_video_drift = Fraction(
        generations[0]["interval"]["exact"]["video_requested_seconds"]
    ) - Fraction(first["exact"]["video_delivered"])
    require(
        abs(first_video_drift) <= Fraction(7, 240),
        f"first delivery video drift exceeded one frame: {float(first_video_drift)}",
    )
    require(
        abs(audio_drift) <= Fraction(7, 240) + abs(first_audio_drift),
        "audio drift grew beyond its bound across cuts",
    )

    tree_status, tree_check = client.request(
        "POST",
        f"/v1/renditions/{generations[-1]['audio_rendition_id']}/integrity-checks",
        {},
    )
    require_equal(tree_status, 201, "repeated cut integrity status")
    require_equal(tree_check["status"], "verified", "repeated cut integrity result")

    # A failed pair must leave both members failed and expose no delivery.
    failure_status, failure_body = client.request(
        "POST",
        "/v1/pairs",
        {
            "audio_asset_id": audio_asset["id"],
            "video_asset_id": video_asset["id"],
            "audio_output_name": "broken.wav",
            "video_output_name": "broken.y4m",
            "sync": {"audio_seconds": 0.0, "video_seconds": 0.0},
            "interval": {"start_seconds": 0.0, "duration_seconds": 0.5},
            "operations": {
                "audio": [],
                "video": [
                    {"type": "video_crop", "x": 0, "y": 0, "width": 66, "height": 36}
                ],
            },
        },
    )
    require_equal(failure_status, 409, "failed pair status")
    require_equal(
        failure_body["error"]["code"],
        "RENDER_FAILED",
        "failed pair error code",
    )
    state = server.application.state_file.read()
    failed = [
        record
        for record in state["renditions"].values()
        if record["output_name"] in {"broken.wav", "broken.y4m"}
    ]
    require_equal(len(failed), 2, "failed pair member count")
    require(
        all(record["status"] == "failed" for record in failed),
        "a failed pair left a successful member",
    )
    require(
        all(record["output_asset_id"] is None for record in failed),
        "a failed pair exposed an output asset",
    )
    require(
        failed[0]["pair_id"] not in state["pairs"]
        or failed[1]["pair_id"] not in state["pairs"],
        "a failed pair was exposed despite missing outputs",
    )
    require(
        not any(
            asset["filename"] in {"broken.wav", "broken.y4m"}
            for asset in state["assets"].values()
        ),
        "a failed pair exposed an output asset record",
    )

    # Ordinary single-media trimming must keep working unchanged.
    standalone = client.create_rendition(
        {
            "primary_asset_id": audio_asset["id"],
            "output_name": "standalone.wav",
            "operations": [
                {"type": "media_trim", "start_seconds": 0.1, "duration_seconds": 0.2}
            ],
        }
    )
    require_equal(standalone["status"], "completed", "standalone status")
    require(standalone.get("pair_id") is None, "single-media rendition joined a pair")
    return {
        "workflow": "pair-derivation",
        "pair_id": pair["id"],
        "generations": len(generations),
        "audio_drift_seconds": float(audio_drift),
        "video_drift_seconds": float(video_drift),
        "relative_drift_seconds": float(relative_drift),
        "failed_pair_code": failure_body["error"]["code"],
        "standalone_pair_id": standalone.get("pair_id"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "workflow",
        choices=(
            "ingest-media-asset",
            "execute-rendition",
            "verify-reproduction",
            "pair-derivation",
        ),
    )
    args = parser.parse_args()

    with running_server() as (client, server):
        if args.workflow == "ingest-media-asset":
            result = run_ingest_media_asset(client)
        elif args.workflow == "execute-rendition":
            result = run_execute_rendition(client)
        elif args.workflow == "pair-derivation":
            result = run_pair_derivation(client, server)
        else:
            result = run_verify_reproduction(client, server)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
