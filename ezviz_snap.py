#!/usr/bin/env python3
"""Capture a JPEG snapshot from every channel of an EZVIZ cloud NVR.

Only login + password required — no local network access, no SDK license.

Usage:
    python3 ezviz_snap.py <SERIAL> [--channels 1,2,3] [--out DIR]

Credentials (any of these):
    --account / --password flags
    EZ_ACC / EZ_PWD environment variables
    .env file with EZ_ACC=... and EZ_PWD=...
"""
from __future__ import annotations
import argparse, os, re, subprocess, sys, time
from pyezvizapi import EzvizClient
from pyezvizapi.api_endpoints import API_ENDPOINT_PAGELIST
import pyezvizapi.cloud_stream as cs
from pyezvizapi.cloud_stream import open_cloud_stream

VPS_PREFIX = b"\x00\x00\x00\x01\x40\x01"


def _load_dotenv():
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
    return c, res


def channels_for(res, serial):
    out = []
    for x in res:
        if x.get("deviceSerial") == serial:
            li = str(x.get("localIndex"))
            if li.isdigit() and int(li) >= 1:
                out.append(int(li))
    return sorted(set(out))


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


def depay_hevc(pkts) -> bytes:
    out = bytearray()
    fu_buf: bytearray | None = None
    fu_hdr = b""
    for p in pkts:
        if len(p) < 2 or (p[1] & 0x7F) != 96:
            continue
        pl = rtp_payload(p)
        if len(pl) < 2:
            continue
        t = (pl[0] >> 1) & 0x3F
        marker = bool(p[1] & 0x80)
        if t < 48:
            fu_buf = None
            out += b"\x00\x00\x00\x01" + pl
        elif t == 48:
            fu_buf = None
            i = 2
            while i + 2 <= len(pl):
                s = int.from_bytes(pl[i:i + 2], "big"); i += 2
                out += b"\x00\x00\x00\x01" + pl[i:i + s]; i += s
        elif t == 49 and len(pl) >= 3:
            f = pl[2]
            if f & 0x80:  # start
                fu_buf = bytearray(pl[3:])
                fu_hdr = bytes([(pl[0] & 0x81) | ((f & 0x3F) << 1), pl[1]])
            elif fu_buf is not None:  # continuation or end
                fu_buf += pl[3:]
                if marker or (f & 0x40):  # end bit
                    out += b"\x00\x00\x00\x01" + fu_hdr + bytes(fu_buf)
                    fu_buf = None
    return bytes(out)


def collect_gop(c, serial, channel, max_packets=2000, max_seconds=30.0, timeout=8.0):
    """Collect packets from the next clean GOP: VPS → SPS → PPS → complete IDR."""
    pkts = []
    vps_idx = -1
    idr_started = False
    idr_done = False
    t0 = time.time()
    with open_cloud_stream(c, serial, channel=channel, refresh_vtm=True, timeout=timeout) as st:
        st.start()
        for b in st.iter_payloads(max_packets=max_packets):
            pkts.append(b)
            if len(b) < 2 or (b[1] & 0x7F) != 96:
                continue
            pl = rtp_payload(b)
            if len(pl) < 2:
                continue
            t = (pl[0] >> 1) & 0x3F
            marker = bool(b[1] & 0x80)

            if t == 32:  # VPS — new GOP boundary, reset
                vps_idx = len(pkts) - 1
                idr_started = False
                idr_done = False

            if vps_idx >= 0:
                if t == 49 and len(pl) >= 3:  # FU
                    fu_type = pl[2] & 0x3F
                    if fu_type in (19, 20) and (pl[2] & 0x80):  # IDR FU-start
                        idr_started = True
                    if idr_started and marker:
                        idr_done = True
                elif t in (19, 20) and marker:  # single-NAL IDR
                    idr_done = True

                if idr_done:
                    break

            if time.time() - t0 > max_seconds:
                break

    if vps_idx >= 0:
        return pkts[vps_idx:]
    return pkts


def grab(c, serial, channel, out_path, retries=1) -> bool:
    for attempt in range(retries + 1):
        try:
            pkts = collect_gop(c, serial, channel)
            h = depay_hevc(pkts)
            i = h.find(VPS_PREFIX)
            if i >= 0:
                h = h[i:]
            tmp = out_path + ".h265"
            with open(tmp, "wb") as f:
                f.write(h)
            r = subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "hevc", "-i", tmp,
                 "-frames:v", "1", "-q:v", "2", out_path],
                capture_output=True, text=True,
            )
            os.remove(tmp)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 3000:
                return True
        except Exception as e:
            sys.stderr.write(f"    ch{channel} attempt {attempt+1} error: {type(e).__name__}: {str(e)[:80]}\n")
        time.sleep(1)
    return False


def main():
    _load_dotenv()

    ap = argparse.ArgumentParser(
        description="Capture JPEG snapshots from every channel of an EZVIZ cloud NVR."
    )
    ap.add_argument("serial", help="NVR device serial number")
    ap.add_argument("--account", default=os.environ.get("EZ_ACC", ""),
                    help="EZVIZ account. Also: EZ_ACC env var.")
    ap.add_argument("--password", default=os.environ.get("EZ_PWD", ""),
                    help="EZVIZ password. Also: EZ_PWD env var.")
    ap.add_argument("--region", default=os.environ.get("EZ_REGION", "apiisgp.ezvizlife.com"),
                    help="API region host (default: apiisgp.ezvizlife.com)")
    ap.add_argument("--name", default=None, help="Human-readable name for output folder")
    ap.add_argument("--channels", default=None, help="Comma-separated channel list (default: all)")
    ap.add_argument("--out", default="snapshots", help="Output directory (default: ./snapshots)")
    args = ap.parse_args()

    if not args.account or not args.password:
        ap.error("--account and --password are required (or set EZ_ACC / EZ_PWD env vars)")

    c, res = login_and_patch(args.account, args.password, args.region)
    name = args.name or args.serial
    outdir = os.path.join(args.out, re.sub(r"[^A-Za-z0-9_]+", "_", name))
    os.makedirs(outdir, exist_ok=True)

    chans = ([int(x) for x in args.channels.split(",") if x.strip()]
             if args.channels else channels_for(res, args.serial))
    print(f"[{name}] {args.serial}: {len(chans)} channels -> {outdir}")

    ok = 0
    for ch in chans:
        p = os.path.join(outdir, f"ch{ch:02d}.jpg")
        t0 = time.time()
        good = grab(c, args.serial, ch, p)
        if good:
            ok += 1
            print(f"  OK  ch{ch:02d}  ({os.path.getsize(p) // 1024} KB, {time.time()-t0:.0f}s)")
        else:
            print(f"  FAIL ch{ch:02d}  ({time.time()-t0:.0f}s)")
    print(f"[{name}] DONE: {ok}/{len(chans)} snapshots -> {outdir}")


if __name__ == "__main__":
    main()
