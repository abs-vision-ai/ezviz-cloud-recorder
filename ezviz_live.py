#!/usr/bin/env python3
"""Live frame stream from EZVIZ cloud NVR — yields decoded BGR frames like a webcam.

Drop-in replacement for cv2.VideoCapture when you only have login + password.

Usage as library:
    from ezviz_live import EzvizLiveStream

    with EzvizLiveStream("SERIAL", 43, account="...", password="...") as stream:
        for frame in stream:          # frame is numpy (H, W, 3) BGR
            result = model(frame)

Usage as script (preview window):
    python3 ezviz_live.py SERIAL CHANNEL --account LOGIN --password PASS
"""
from __future__ import annotations
import argparse, os, queue, struct, subprocess, sys, threading, time
from typing import Iterator

import numpy as np

from pyezvizapi import EzvizClient
from pyezvizapi.api_endpoints import API_ENDPOINT_PAGELIST
import pyezvizapi.cloud_stream as cs
from pyezvizapi.cloud_stream import open_cloud_stream

# iOS client type + VIP relay priority
_orig_build = cs.build_vtm_url
def _patched_build(host, port, serial, sbiz, vtdu, *, channel=1, client_type=9, timestamp_ms=None):
    url = _orig_build(host, port, serial, sbiz, vtdu,
                      channel=channel, client_type=1, timestamp_ms=timestamp_ms)
    return url.replace("vip=0", "vip=1")
cs.build_vtm_url = _patched_build

VPS_PREFIX = b"\x00\x00\x00\x01\x40\x01"

# Main stream resolution — used to calculate raw frame size
MAIN_W, MAIN_H = 3200, 1800


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


def _login(account: str, password: str, region: str) -> EzvizClient:
    c = EzvizClient(account, password, region)
    c.login()
    base = "https://" + c._token["api_url"] + API_ENDPOINT_PAGELIST
    res, vtm, off = [], {}, 0
    while True:
        j = c._session.get(base, params={"filter": "VTM", "offset": off, "limit": 30}, timeout=20).json()
        r = j.get("resourceInfos") or []
        res += r; vtm.update(j.get("VTM") or {})
        if not j.get("page", {}).get("hasNext") or not r:
            break
        off += len(r)
    cs.get_vtm_page_list = lambda cl: {"resourceInfos": res, "VTM": vtm}
    return c


def _rtp_payload(p: bytes) -> bytes:
    if len(p) < 12: return b""
    b0 = p[0]; o = 12 + (b0 & 0x0F) * 4
    if b0 & 0x10:
        if o + 4 > len(p): return b""
        o += 4 + int.from_bytes(p[o + 2:o + 4], "big") * 4
    e = len(p) - (p[-1] if (b0 & 0x20) else 0)
    return p[o:e]


def _depay_one(p: bytes) -> bytes:
    pl = _rtp_payload(p)
    if len(pl) < 2: return b""
    t = (pl[0] >> 1) & 0x3F
    if t < 48: return b"\x00\x00\x00\x01" + pl
    if t == 48:
        out = bytearray(); i = 2
        while i + 2 <= len(pl):
            s = int.from_bytes(pl[i:i + 2], "big"); i += 2
            out += b"\x00\x00\x00\x01" + pl[i:i + s]; i += s
        return bytes(out)
    if t == 49 and len(pl) >= 3:
        f = pl[2]
        if f & 0x80:
            return b"\x00\x00\x00\x01" + bytes([(pl[0] & 0x81) | ((f & 0x3F) << 1), pl[1]]) + pl[3:]
        return pl[3:]
    return b""


def _vtm_writer(account, password, region, serial, channel, ff_stdin, stop_event):
    """Background thread: VTM → HEVC Annex-B → ffmpeg stdin."""
    c = _login(account, password, region)
    reconnects = 0
    while not stop_event.is_set():
        try:
            with open_cloud_stream(c, serial, channel=channel, refresh_vtm=True, timeout=12.0) as st:
                st.start()
                synced = False
                frame_buf = bytearray()
                for pkt in st.iter_payloads(max_packets=None):
                    if stop_event.is_set():
                        return
                    if len(pkt) < 2 or (pkt[1] & 0x7F) != 96:
                        continue
                    nal = _depay_one(pkt)
                    marker = bool(pkt[1] & 0x80)
                    if not synced:
                        if VPS_PREFIX in nal:
                            synced = True; frame_buf = bytearray(nal)
                        elif marker:
                            frame_buf = bytearray()
                        continue
                    if nal:
                        frame_buf += nal
                    if marker and frame_buf:
                        try:
                            ff_stdin.write(bytes(frame_buf))
                            ff_stdin.flush()
                        except (BrokenPipeError, OSError):
                            return
                        frame_buf = bytearray()
        except Exception as e:
            if stop_event.is_set():
                return
            reconnects += 1
            sys.stderr.write(f"  [ezviz_live] reconnect #{reconnects}: {e}\n")
            time.sleep(2)
            try:
                c = _login(account, password, region)
            except Exception:
                pass


class EzvizLiveStream:
    """Live frame generator from an EZVIZ cloud camera channel.

    Example:
        with EzvizLiveStream("FK2335516", 43, account="...", password="...") as stream:
            for frame in stream:
                cv2.imshow("live", frame)
                if cv2.waitKey(1) == 27:
                    break
    """

    def __init__(
        self,
        serial: str,
        channel: int,
        *,
        account: str = "",
        password: str = "",
        region: str = "apiisgp.ezvizlife.com",
        width: int = MAIN_W,
        height: int = MAIN_H,
        fps: int = 15,
        queue_size: int = 4,
    ):
        _load_dotenv()
        self.serial = serial
        self.channel = channel
        self.account = account or os.environ.get("EZ_ACC", "")
        self.password = password or os.environ.get("EZ_PWD", "")
        self.region = region
        self.width = width
        self.height = height
        self.fps = fps
        self.queue_size = queue_size
        self._stop = threading.Event()
        self._ff: subprocess.Popen | None = None
        self._writer_thread: threading.Thread | None = None

    def __enter__(self) -> "EzvizLiveStream":
        self._start()
        return self

    def __exit__(self, *_):
        self.stop()

    def _start(self):
        self._stop.clear()
        self._ff = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "hevc", "-r", str(self.fps), "-i", "pipe:0",
             "-f", "rawvideo", "-pix_fmt", "bgr24",
             "-vf", f"scale={self.width}:{self.height}",
             "pipe:1"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        self._writer_thread = threading.Thread(
            target=_vtm_writer,
            args=(self.account, self.password, self.region,
                  self.serial, self.channel,
                  self._ff.stdin, self._stop),
            daemon=True,
        )
        self._writer_thread.start()

    def stop(self):
        self._stop.set()
        if self._ff:
            try: self._ff.stdin.close()
            except Exception: pass
            try: self._ff.kill()
            except Exception: pass
        if self._writer_thread:
            self._writer_thread.join(timeout=5)

    def __iter__(self) -> Iterator[np.ndarray]:
        assert self._ff is not None, "Use as context manager: `with EzvizLiveStream(...) as s:`"
        frame_bytes = self.width * self.height * 3
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._ff.stdout.read(frame_bytes - len(buf))
                if not chunk:
                    break
                buf += chunk
                if len(buf) >= frame_bytes:
                    frame = np.frombuffer(buf[:frame_bytes], dtype=np.uint8).reshape(
                        self.height, self.width, 3
                    )
                    yield frame.copy()
                    buf = buf[frame_bytes:]
            except Exception:
                break


def main():
    _load_dotenv()
    ap = argparse.ArgumentParser(description="Preview live EZVIZ stream (requires OpenCV).")
    ap.add_argument("serial")
    ap.add_argument("channel", type=int)
    ap.add_argument("--account", default=os.environ.get("EZ_ACC", ""))
    ap.add_argument("--password", default=os.environ.get("EZ_PWD", ""))
    ap.add_argument("--region", default=os.environ.get("EZ_REGION", "apiisgp.ezvizlife.com"))
    ap.add_argument("--width", type=int, default=MAIN_W)
    ap.add_argument("--height", type=int, default=MAIN_H)
    ap.add_argument("--fps", type=int, default=15)
    args = ap.parse_args()

    if not args.account or not args.password:
        ap.error("--account and --password required (or EZ_ACC / EZ_PWD env vars)")

    try:
        import cv2
    except ImportError:
        ap.error("opencv-python not installed: pip install opencv-python")

    print(f"Connecting to {args.serial} ch{args.channel}...")
    t0 = time.time()
    frames = 0
    with EzvizLiveStream(args.serial, args.channel,
                         account=args.account, password=args.password,
                         region=args.region,
                         width=args.width, height=args.height, fps=args.fps) as stream:
        for frame in stream:
            frames += 1
            fps_actual = frames / (time.time() - t0)
            cv2.putText(frame, f"{fps_actual:.1f} fps", (20, 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 255, 0), 2)
            cv2.imshow(f"EZVIZ {args.serial} ch{args.channel}", frame)
            if cv2.waitKey(1) & 0xFF == 27:  # ESC
                break
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
