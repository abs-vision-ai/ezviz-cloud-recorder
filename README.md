# ezviz-cloud-recorder

Record clean full-resolution video from EZVIZ cloud NVR cameras using only a login and password — no local network access, no SDK license, no extra hardware.

## The Problem

EZVIZ NVR cameras stream at up to 3200×1800 (H.265) through their VTM cloud relay. Naive RTP depayloading produces heavily corrupted video with green blocks, missing frames, and decoder errors like `Invalid NAL unit` and `missing picture in access unit`.

## Root Cause & Fix

The EZVIZ VTM relay injects **PT=112 control packets** (RTP payload type 112) into the video stream. These packets carry no video payload but have the **RTP marker bit set** (`marker=True`). A standard RTP depayloader interprets the marker bit as "end of frame" and flushes the frame buffer — but these packets arrive mid-FU-sequence (in the middle of a fragmented large IDR frame), causing the assembler to emit a partial, corrupted frame.

**One-line fix:**
```python
if len(pkt) < 2 or (pkt[1] & 0x7F) != 96:
    continue  # skip PT=112 control packets, only process PT=96 HEVC
```

This eliminates all corruption on the main stream (3200×1800). Sub-stream (640×360) is less affected because its smaller IDR frames complete before a control packet arrives.

## Requirements

- Python 3.9+
- `ffmpeg` in PATH
- `pip install pyezvizapi`

## Setup

```bash
pip install pyezvizapi
cp .env.example .env
# edit .env with your EZVIZ credentials
```

## Usage

### Record video

```bash
# Single file, 10 minutes:
python3 ezviz_record.py <SERIAL> <CHANNEL> --out output.mp4 --seconds 600

# 30-minute segments, run forever:
python3 ezviz_record.py <SERIAL> <CHANNEL> \
    --out-dir ./recordings --prefix cam1 --segment-time 1800 --seconds 0

# With credentials as flags:
python3 ezviz_record.py <SERIAL> <CHANNEL> \
    --account your@email.com --password yourpass --out output.mp4
```

### Snapshot all channels

```bash
python3 ezviz_snap.py <SERIAL> --out snapshots/
```

### Find your NVR serial number

Log in to the EZVIZ app → Device settings → Device serial number. It looks like `FK2335516` or `E20692995`.

### Regions

| Region | Host |
|--------|------|
| Asia/Singapore (default) | `apiisgp.ezvizlife.com` |
| Europe | `apiieu.ezvizlife.com` |
| United States | `apiius.ezvizlife.com` |

Set via `--region` flag or `EZ_REGION` env var.

## How it works

1. **Login** to EZVIZ cloud API with account + password
2. **Paginate VTM resource list** — the server returns max 30 devices per page; must step `offset += len(returned)` to get all
3. **Open VTM stream** (TCP connection to `vtm*.ezvizlife.com:8554`) — VTM is EZVIZ's cloud relay that proxies the NVR stream
4. **Filter PT=96** — discard PT=112 control packets (the corruption fix)
5. **Depayload RTP/HEVC** — handle single NAL (type<48), aggregation packets (AP, type=48), and fragmentation units (FU, type=49) per RFC 7798
6. **Sync on VPS** — wait for a VPS NAL (`\x00\x00\x00\x01\x40\x01`) before writing to ffmpeg; ensures every recording starts at a clean GOP boundary
7. **Pipe to ffmpeg** — write Annex-B HEVC to ffmpeg stdin; use `-r 15` to generate monotonic timestamps; mux to MP4 with `-c copy` (zero re-encoding)

## Output quality

- **Resolution:** 3200×1800 (full main stream, camera-dependent)
- **Codec:** H.265 / HEVC, passthrough (no re-encoding)
- **Bitrate:** ~4–8 Mbps (camera-dependent)
- **Corruption:** none (with PT=112 fix)
