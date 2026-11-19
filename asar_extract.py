#!/usr/bin/env python3
"""asar_extract.py - minimal ASAR archive reader (no npm deps).

ASAR layout: [4-byte uint32 header_size][8-byte pickle][4-byte json_size]
             [json header][file data...]

Usage:
    python asar_extract.py app.asar outdir
    python asar_extract.py app.asar --list
"""

import json
import os
import struct
import sys


def read_header(fh):
    fh.seek(0)
    raw = fh.read(16)
    if len(raw) < 16:
        raise ValueError("truncated asar")
    # 4-byte size, 4-byte header-size pickle, 4-byte payload-size, 4-byte json-size
    _, _, _, json_size = struct.unpack("<IIII", raw)
    header = fh.read(json_size).decode("utf-8", "replace")
    base = 16 + json_size
    return json.loads(header), base


def walk(node, prefix, out):
    for name, meta in node.get("files", {}).items():
        path = f"{prefix}/{name}" if prefix else name
        if "files" in meta:
            walk(meta, path, out)
        else:
            out.append({
                "path": path,
                "size": int(meta.get("size", 0)),
                "offset": int(meta.get("offset", 0)) if meta.get("offset") else None,
                "unpacked": bool(meta.get("unpacked")),
                "link": meta.get("link"),
                "executable": bool(meta.get("executable")),
            })


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    asar = argv[1]
    listing_only = "--list" in argv
    outdir = next((a for a in argv[2:] if not a.startswith("-")), "asar_out")

    with open(asar, "rb") as fh:
        header, base = read_header(fh)
        entries = []
        walk(header, "", entries)

        if listing_only:
            for e in entries:
                tag = "  [unpacked]" if e["unpacked"] else ""
                print(f"{e['size']:>10}  {e['path']}{tag}")
            print(f"\n{len(entries)} entries")
            return 0

        os.makedirs(outdir, exist_ok=True)
        written = skipped = 0
        for e in entries:
            dest = os.path.join(outdir, e["path"].replace("/", os.sep))
            if e["link"]:
                continue
            if e["unpacked"]:
                skipped += 1
                continue
            os.makedirs(os.path.dirname(dest) or outdir, exist_ok=True)
            if not e["size"]:
                open(dest, "wb").close()
                written += 1
                continue
            fh.seek(base + e["offset"])
            remaining = e["size"]
            with open(dest, "wb") as out:
                while remaining > 0:
                    chunk = fh.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
            written += 1
        print(f"extracted {written} files to {outdir} ({skipped} unpacked left in place)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
