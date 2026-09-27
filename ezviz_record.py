#!/usr/bin/env python3
"""Record clean full-resolution (main stream) video from EZVIZ cloud NVR.

Only login + password required — no local network access, no SDK license.

Key discovery: EZVIZ VTM relay injects PT=112 control packets with marker=True
into the RTP stream. These arrive mid-FU-sequence and corrupt frame reassembly.
Filtering to PT=96 (HEVC) only eliminates all corruption.

Usage:
    # Single file, 10 minutes:
    python3 ezviz_record.py <SERIAL> <CHANNEL> --out output.mp4 --seconds 600

    # Hourly segments, run forever:
    python3 ezviz_record.py <SERIAL> <CHANNEL> \\
        --out-dir ./recordings --prefix cam1 --segment-time 3600 --seconds 0

Credentials (any of these):
    --account / --password flags
    EZ_ACC / EZ_PWD environment variables
    .env file with EZ_ACC=... and EZ_PWD=...

Region defaults to Singapore (apiisgp.ezvizlife.com). Other regions:
    EU:  apiieu.ezvizlife.com
    US:  apiius.ezvizlife.com
"""
from __future__ import annotations
import argparse, os, subprocess, sys, time
from pyezvizapi import EzvizClient
from pyezvizapi.api_endpoints import API_ENDPOINT_PAGELIST
import pyezvizapi.cloud_stream as cs
from pyezvizapi.cloud_stream import open_cloud_stream

# Request main stream (stream=1) with iOS client type and VIP relay priority.
# The PT=112 corruption fix is in the main loop below, not here.
_orig_build = cs.build_vtm_url
def _patched_build(host, port, serial, sbiz, vtdu, *, channel=1, client_type=9, timestamp_ms=None):
    url = _orig_build(host, port, serial, sbiz, vtdu,
                      channel=channel, client_type=1, timestamp_ms=timestamp_ms)
    return url.replace("vip=0", "vip=1")
cs.build_vtm_url = _patched_build

VPS_PREFIX = b"\x00\x00\x00\x01\x40\x01"   # HEVC VPS NAL (type 32)


def _load_dotenv():
    """Load .env file from current directory if present."""
    try:
        with open(".env") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


def login_and_patch(account: str, password: str, region: str) -> EzvizClient:
    c = EzvizClient(account, password, region)
    c.login()
    # Paginate VTM resource list (server returns max 30 per page)
    base = "https://" + c._token["api_url"] + API_ENDPOINT_PAGELIST
    res, vtm, off = [], {}, 0
    while True:
        j = c._session.get(base, params={"filter": "VTM", "offset": off, "limit": 30}, timeout=20).json()
        r = j.get("resourceInfos") or []
        res += r
        vtm.update(j.get("VTM") or {})
        if not j.get("page", {}).get("hasNext") or not r:
            break
        off += len(r)
    cs.get_vtm_page_list = lambda cl: {"resourceInfos": res, "VTM": vtm}
    return c


def rtp_payload(p: bytes) -> bytes:
    if len(p) < 12:
        return b""
    b0 = p[0]
    o = 12 + (b0 & 0x0F) * 4
    if b0 & 0x10:
        if o + 4 > len(p):
            return b""
        o += 4 + int.from_bytes(p[o + 2:o + 4], "big") * 4
    e = len(p) - (p[-1] if (b0 & 0x20) else 0)
    return p[o:e]


def depay_one(p: bytes) -> bytes:
    """Decode one RTP packet to Annex-B HEVC fragment (stateless)."""
    pl = rtp_payload(p)
    if len(pl) < 2:
        return b""
    t = (pl[0] >> 1) & 0x3F
    if t < 48:                          # single NAL unit
        return b"\x00\x00\x00\x01" + pl
    if t == 48:                         # aggregation packet (AP)
        out = bytearray()
        i = 2
        while i + 2 <= len(pl):
            s = int.from_bytes(pl[i:i + 2], "big"); i += 2
            out += b"\x00\x00\x00\x01" + pl[i:i + s]; i += s
        return bytes(out)
    if t == 49 and len(pl) >= 3:        # fragmentation unit (FU)
        f = pl[2]
        if f & 0x80:                    # FU start — reconstruct NAL header
            return b"\x00\x00\x00\x01" + bytes([(pl[0] & 0x81) | ((f & 0x3F) << 1), pl[1]]) + pl[3:]
        return pl[3:]                   # FU continuation / end
    return b""


def main():
    _load_dotenv()

    ap = argparse.ArgumentParser(
        description="Record clean HEVC video from EZVIZ cloud NVR (main stream).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("serial", help="NVR device serial number")
    ap.add_argument("channel", type=int, help="NVR channel number (e.g. 43)")
    ap.add_argument("--account", default=os.environ.get("EZ_ACC", ""),
                    help="EZVIZ account (phone/email). Also: EZ_ACC env var.")
    ap.add_argument("--password", default=os.environ.get("EZ_PWD", ""),
                    help="EZVIZ password. Also: EZ_PWD env var.")
    ap.add_argument("--region", default=os.environ.get("EZ_REGION", "apiisgp.ezvizlife.com"),
                    help="API region host (default: apiisgp.ezvizlife.com)")
    ap.add_argument("--out", default=None, help="Output file (single-file mode)")
    ap.add_argument("--out-dir", default=None, help="Output directory (segment mode)")
    ap.add_argument("--prefix", default=None, help="Segment filename prefix")
    ap.add_argument("--segment-time", type=int, default=0,
                    help="Segment length in seconds, 0 = single file (default: 0)")
    ap.add_argument("--seconds", type=int, default=600,
                    help="Total recording duration in seconds, 0 = infinite (default: 600)")
    args = ap.parse_args()

    if not args.account or not args.password:
        ap.error("--account and --password are required (or set EZ_ACC / EZ_PWD env vars)")

    if args.segment_time > 0:
        outdir = args.out_dir or os.path.dirname(args.out or "") or "."
        os.makedirs(outdir, exist_ok=True)
        prefix = args.prefix or f"{args.serial}_ch{args.channel}"
        pattern = os.path.join(outdir, prefix + "_%Y%m%d_%H%M%S.mp4")
        out_args = ["-c", "copy", "-f", "segment", "-segment_time", str(args.segment_time),
                    "-segment_format", "mp4", "-reset_timestamps", "1", "-strftime", "1", pattern]
    else:
        if not args.out:
            ap.error("--out is required in single-file mode")
        out_args = ["-c", "copy", "-movflags", "+faststart", "-y", args.out]

    ff_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
              "-f", "hevc", "-r", "15", "-i", "pipe:0"] + out_args

    def start_ffmpeg():
        return subprocess.Popen(ff_cmd, stdin=subprocess.PIPE)

    c = login_and_patch(args.account, args.password, args.region)
    ff = start_ffmpeg()
    ff_restarts = 0
    t0 = time.time()
    frames = 0
    reconnects = 0

    try:
        while args.seconds == 0 or (time.time() - t0 < args.seconds):
            try:
                with open_cloud_stream(c, args.serial, channel=args.channel,
                                       refresh_vtm=True, timeout=12.0) as st:
                    st.start()
                    synced = False
                    frame_buf = bytearray()

                    for pkt in st.iter_payloads(max_packets=None):
                        if ff.poll() is not None:
                            ff_restarts += 1
                            sys.stderr.write(f"  ffmpeg restart #{ff_restarts}\n")
                            ff = start_ffmpeg()
                            synced = False
                            frame_buf = bytearray()

                        # KEY FIX: skip PT=112 EZVIZ control packets.
                        # They carry marker=True with no payload and arrive mid-FU-sequence,
                        # causing the assembler to flush a partial frame → corruption.
                        # Only PT=96 carries HEVC RTP data.
                        if len(pkt) < 2 or (pkt[1] & 0x7F) != 96:
                            continue

                        nal = depay_one(pkt)
                        marker = bool(pkt[1] & 0x80)

                        # Wait for VPS before writing — drops any partial leading GOP
                        if not synced:
                            if VPS_PREFIX in nal:
                                synced = True
                                frame_buf = bytearray(nal)
                            elif marker:
                                frame_buf = bytearray()
                            continue

                        if nal:
                            frame_buf += nal

                        if marker and frame_buf:
                            try:
                                ff.stdin.write(bytes(frame_buf))
                                ff.stdin.flush()
                                frames += 1
                            except BrokenPipeError:
                                ff_restarts += 1
                                sys.stderr.write(f"  BrokenPipe restart #{ff_restarts}\n")
                                ff = start_ffmpeg()
                                synced = False
                            frame_buf = bytearray()

                        if args.seconds and time.time() - t0 >= args.seconds:
                            break

            except Exception as e:
                reconnects += 1
                sys.stderr.write(f"  reconnect #{reconnects}: {type(e).__name__}: {str(e)[:80]}\n")
                time.sleep(1)
                try:
                    c = login_and_patch(args.account, args.password, args.region)
                except Exception:
                    pass

    finally:
        try:
            ff.stdin.close()
        except Exception:
            pass
        try:
            ff.wait(timeout=15)
        except Exception:
            ff.kill()

    dur = time.time() - t0
    tail = args.out if args.out else (args.out_dir or "") + f" ({args.prefix or ''}_*.mp4)"
    print(f"[{args.serial} ch{args.channel}] {frames} frames, {dur:.0f}s, "
          f"reconnects={reconnects}, ffmpeg_restarts={ff_restarts} -> {tail}")


if __name__ == "__main__":
    main()
