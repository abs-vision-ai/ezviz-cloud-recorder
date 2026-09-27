#!/usr/bin/env python3
"""List all NVR devices and channels linked to an EZVIZ account.

Usage:
    python3 ezviz_devices.py --account LOGIN --password PAROL

    # Then record from a discovered channel:
    python3 ezviz_record.py <SERIAL> <CHANNEL> --account LOGIN --password PAROL ...

Credentials (any of these):
    --account / --password flags
    EZ_ACC / EZ_PWD environment variables
    .env file with EZ_ACC=... and EZ_PWD=...
"""
from __future__ import annotations
import argparse, os
from pyezvizapi import EzvizClient
from pyezvizapi.api_endpoints import API_ENDPOINT_PAGELIST
import pyezvizapi.cloud_stream as cs


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


def list_devices(account: str, password: str, region: str):
    c = EzvizClient(account, password, region)
    c.login()

    base = "https://" + c._token["api_url"] + API_ENDPOINT_PAGELIST
    resources = []
    off = 0
    while True:
        j = c._session.get(base, params={"filter": "VTM", "offset": off, "limit": 30}, timeout=20).json()
        r = j.get("resourceInfos") or []
        resources += r
        if not j.get("page", {}).get("hasNext") or not r:
            break
        off += len(r)

    # Group channels by NVR (superDeviceSerial)
    nvrs: dict[str, dict] = {}
    for res in resources:
        nvr_serial = res.get("superDeviceSerial") or res.get("deviceSerial", "")
        ch = res.get("localIndex", "0")
        name = res.get("resourceName", "")
        state = res.get("globalState", 0)

        if nvr_serial not in nvrs:
            nvrs[nvr_serial] = {"name": "", "channels": []}

        if str(ch) == "0":
            nvrs[nvr_serial]["name"] = name
        else:
            nvrs[nvr_serial]["channels"].append({
                "ch": int(ch),
                "name": name,
                "online": state == 1,
            })

    print(f"Found {len(nvrs)} NVR(s)\n")
    for serial, info in nvrs.items():
        nvr_name = info["name"] or serial
        channels = sorted(info["channels"], key=lambda x: x["ch"])
        print(f"NVR: {nvr_name}")
        print(f"  Serial : {serial}")
        print(f"  Channels ({len(channels)}):")
        for ch in channels:
            status = "online" if ch["online"] else "offline"
            print(f"    ch{ch['ch']:02d}  {ch['name']:<20}  [{status}]")
        print()
        print(f"  # Record channel example:")
        if channels:
            first_ch = channels[0]["ch"]
            print(f"  python3 ezviz_record.py {serial} {first_ch} "
                  f"--out-dir recordings --segment-time 1800 --seconds 0")
        print()


def main():
    _load_dotenv()

    ap = argparse.ArgumentParser(
        description="List all NVR devices and channels linked to an EZVIZ account."
    )
    ap.add_argument("--account", default=os.environ.get("EZ_ACC", ""),
                    help="EZVIZ account (phone/email). Also: EZ_ACC env var.")
    ap.add_argument("--password", default=os.environ.get("EZ_PWD", ""),
                    help="EZVIZ password. Also: EZ_PWD env var.")
    ap.add_argument("--region", default=os.environ.get("EZ_REGION", "apiisgp.ezvizlife.com"),
                    help="API region host (default: apiisgp.ezvizlife.com)")
    args = ap.parse_args()

    if not args.account or not args.password:
        ap.error("--account and --password are required (or set EZ_ACC / EZ_PWD env vars)")

    list_devices(args.account, args.password, args.region)


if __name__ == "__main__":
    main()
