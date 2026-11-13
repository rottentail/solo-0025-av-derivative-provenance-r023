# AV Derivative Provenance

AV Derivative Provenance is a Python backend for deterministic audio and video
derivation. It accepts PCM WAV and planar Y4M media, applies bounded
operations, records content digests and lineage, and reproduces a completed
result on demand to verify that the stored bytes still match the plan.

The application uses only the Python standard runtime. It does not need
FFmpeg, network access, credentials, or an online database.

## What it does

- Ingests one- or two-channel signed 16-bit PCM WAV.
- Ingests planar 4:2:0 YUV4MPEG2 in the `C420` family.
- Applies audio trim, gain, normalization, and two-source mixing.
- Applies video trim, even-aligned crop, luma offset, and horizontal mirror.
- Delivers paired WAV/Y4M derivatives under one declared sync point and shared
  interval, with exact rational timing that does not drift across repeated
  cuts.
- Persists immutable assets, rendition state, provenance edges, media pairs,
  and integrity checks.
- Replays the stored operation plan and compares both bytes and media
  descriptors; pair checks replay both members together.

## Quick start

```bash
python3 -m compileall -q avprovenance scripts
python3 -m avprovenance --host 127.0.0.1 --port 8765 --data-dir .avprovenance-data
```

The startup line includes the active host, port, and data directory. Stop the
process with `Ctrl+C`.

## HTTP endpoints

### Health

```http
GET /health
```

### Upload an asset

```http
POST /v1/assets
Content-Type: application/json

{
  "filename": "source.wav",
  "data_base64": "<strict base64>"
}
```

### Create a synchronized pair

```http
POST /v1/pairs
Content-Type: application/json

{
  "audio_asset_id": "asset_...",
  "video_asset_id": "asset_...",
  "audio_output_name": "delivery.wav",
  "video_output_name": "delivery.y4m",
  "sync": {"audio_seconds": 0.0, "video_seconds": 0.0},
  "interval": {"start_seconds": 0.5, "duration_seconds": 2.0},
  "operations": {
    "audio": [{"type": "audio_gain", "db": -3}],
    "video": [{"type": "video_luma_offset", "amount": 7}]
  }
}
```

`sync` states which position in each source lines up at pair time zero;
`interval` is the shared playback range relative to that alignment. Both
outputs are produced before anything is committed, so a failed pair returns
`409 RENDER_FAILED` with both member renditions `failed` and no visible
delivery. The response `interval` block is the single interpretation of the
range: `start_seconds` and `duration_seconds` are shared, while
`members.audio`/`members.video` record the delivered sample or frame
positions. Fractional frame rates are handled as exact rationals, so the
audio/video offset stays within half a sample and one frame no matter how
many times a delivery is cut.

### Read a pair

```http
GET /v1/pairs/{pair_id}
GET /v1/renditions/{rendition_id}/pair
```

### Cut a pair through either member

Pass a pair output asset as `primary_asset_id` and include a `media_trim`. The
same shared interval is applied to both members, so the counterpart advances
with the requesting one instead of staying at its old position:

```json
{
  "primary_asset_id": "asset_from_pair_wav",
  "output_name": "child.wav",
  "operations": [
    {"type": "media_trim", "start_seconds": 0.2, "duration_seconds": 0.5},
    {"type": "audio_gain", "db": -2}
  ]
}
```

For a video member, name the trim in frames (`start_frame`, `frame_count`).
Extra operations apply only to the requesting member. The counterpart output
name may be supplied as `counterpart_output_name`. A request without
`media_trim` is treated as ordinary single-media derivation.

### Read an asset

```http
GET /v1/assets/{asset_id}
```

### Create a rendition

```http
POST /v1/renditions
Content-Type: application/json

{
  "primary_asset_id": "asset_...",
  "secondary_asset_id": null,
  "output_name": "derived.wav",
  "operations": [
    {
      "type": "media_trim",
      "start_seconds": 0.1,
      "duration_seconds": 0.3
    },
    {"type": "audio_gain", "db": -3},
    {"type": "audio_normalize", "target_peak": 0.8}
  ]
}
```

Rendition creation is synchronous at the HTTP boundary. The durable transition
history still records `queued`, `running`, and the terminal state.

Audio-only operations:

- `media_trim` with `start_seconds` and `duration_seconds`
- `audio_gain` with `db`
- `audio_normalize` with `target_peak`
- `audio_mix` with `secondary_gain`, `primary_weight`, and `secondary_weight`

Video-only operations:

- `media_trim` with `start_frame` and `frame_count`
- `video_crop` with even `x`, `y`, `width`, and `height`
- `video_luma_offset` with `amount`
- `video_mirror`

### Read rendition state

```http
GET /v1/renditions/{rendition_id}
```

### Read lineage

```http
GET /v1/renditions/{rendition_id}/lineage
```

### Verify reproduction

```http
POST /v1/renditions/{rendition_id}/integrity-checks
Content-Type: application/json

{}
```

The successful response has `status: "verified"`. A byte or descriptor
difference returns HTTP `409` with code `INTEGRITY_MISMATCH`; the mismatch
record remains durable. When the rendition belongs to a pair, the check
replays both members and returns one `verified`/`mismatch` entry per member, so
a single medium can never be reported verified on its own.

## Media operation examples

Audio mixing plan:

```json
[
  {"type": "media_trim", "start_seconds": 0.05, "duration_seconds": 0.3},
  {"type": "audio_gain", "db": -3},
  {
    "type": "audio_mix",
    "secondary_gain": -6,
    "primary_weight": 0.75,
    "secondary_weight": 0.4
  },
  {"type": "audio_normalize", "target_peak": 0.8}
]
```

Video plan:

```json
[
  {"type": "media_trim", "start_frame": 1, "frame_count": 4},
  {
    "type": "video_crop",
    "x": 8,
    "y": 6,
    "width": 32,
    "height": 18
  },
  {"type": "video_luma_offset", "amount": 12},
  {"type": "video_mirror"}
]
```

## Data layout

```text
.avprovenance-data/
├── state.json
└── content/
    └── <digest-prefix>/
        └── <digest-prefix>/
            └── <sha256>
```

`state.json` is replaced atomically. Binary objects are content-addressed by
SHA-256 and written through temporary siblings before replacement.

## Source layout

```text
avprovenance/
├── application.py
├── http_api.py
├── models.py
├── validation.py
├── media/
├── renditions/
├── storage/
└── transport/
```

The HTTP boundary calls the application layer. The application coordinates
plan handling, media operations, and persistence. The media layer does not
depend on HTTP or the repository.

## Workflow checks

The baseline intentionally has no automated test suite. Tests are deferred to
the later task stage, which will add red/green verification coverage. The
current production workflow checks are:

```bash
python3 scripts/workflow_checks.py ingest-media-asset
python3 scripts/workflow_checks.py execute-rendition
python3 scripts/workflow_checks.py verify-reproduction
python3 scripts/workflow_checks.py pair-derivation
```

Each check starts a temporary HTTP server, generates media bytes in memory,
exercises the public endpoints, and shuts the server down.

## Environment

No environment variables are required. Runtime choices use `--host`, `--port`,
and `--data-dir`. Incoming request bodies are limited to 36 MiB, decoded media
payloads to 24 MiB, WAV geometry to one or two channels, and Y4M geometry to
1920x1080 with at most 240 frames.
