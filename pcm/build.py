#!/usr/bin/env python3
"""Build a KiCad Plugin and Content Manager (PCM) repository from the plugins
in this repo.

    python3 pcm/build.py --base-url https://plus381-robotics.github.io/kicad-plugins/

Output in dist/ (published to GitHub Pages by .github/workflows/pcm.yml):
    repository.json   <- the URL people add in KiCad's PCM
    packages.json
    resources.zip     (package icons)
    packages/<identifier>-<version>.zip

Each plugin folder needs plugin.json, a plus381/__init__.py-style __version__
(or a "version" entry in pcm.json) and a pcm.json with the package metadata.
"""

import argparse
import hashlib
import io
import json
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {"__pycache__", ".pytest_cache", "pcm.json"}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def plugin_dirs():
    return sorted(p.parent for p in ROOT.glob("*/plugin.json") if (p.parent / "pcm.json").exists())


def version_of(d, meta):
    if meta.get("version"):
        return meta["version"]
    for init in d.glob("*/__init__.py"):
        m = re.search(r'__version__\s*=\s*"([^"]+)"', init.read_text())
        if m:
            return m.group(1)
    raise SystemExit(f"{d.name}: no version (set __version__ or 'version' in pcm.json)")


def build_package(d, base_url, out):
    meta = json.loads((d / "pcm.json").read_text())
    version = version_of(d, meta)
    ident = meta["identifier"]
    files = [p for p in sorted(d.rglob("*")) if p.is_file() and not (set(p.relative_to(d).parts) & SKIP)
             and p.suffix != ".pyc"]

    pkg_meta = {k: v for k, v in meta.items() if k not in ("version", "icon", "status", "kicad_version")}
    pkg_meta["versions"] = [{"version": version, "status": meta.get("status", "stable"),
                             "kicad_version": meta.get("kicad_version", "10.0"), "runtime": "ipc"}]

    buf = io.BytesIO()
    install_size = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("metadata.json", json.dumps(pkg_meta, indent=2))
        for f in files:
            data = f.read_bytes()
            install_size += len(data)
            z.writestr("plugins/" + f.relative_to(d).as_posix(), data)
        icon = d / meta.get("icon", "icons/plugin-64.png")
        if icon.exists():
            z.writestr("resources/icon.png", icon.read_bytes())
    data = buf.getvalue()
    name = f"{ident}-{version}.zip"
    (out / "packages").mkdir(parents=True, exist_ok=True)
    (out / "packages" / name).write_bytes(data)

    entry = dict(pkg_meta)
    entry["versions"] = [dict(pkg_meta["versions"][0], download_url=base_url + "packages/" + name,
                              download_sha256=sha256(data), download_size=len(data), install_size=install_size)]
    icon_bytes = icon.read_bytes() if icon.exists() else None
    return entry, ident, icon_bytes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True, help="public URL where dist/ will be served, ending in /")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    args = ap.parse_args()
    base = args.base_url.rstrip("/") + "/"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    packages = []
    res = io.BytesIO()
    with zipfile.ZipFile(res, "w", zipfile.ZIP_DEFLATED) as z:
        for d in plugin_dirs():
            entry, ident, icon = build_package(d, base, out)
            packages.append(entry)
            if icon:
                z.writestr(f"{ident}/icon.png", icon)
            print(f"built {ident} {entry['versions'][0]['version']}")

    now = int(time.time())
    utc = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    pkgs = json.dumps({"packages": packages}, indent=2).encode()
    (out / "packages.json").write_bytes(pkgs)
    (out / "resources.zip").write_bytes(res.getvalue())
    repo = {
        "$schema": "https://go.kicad.org/pcm/schemas/v2#/definitions/Repository",
        "name": "Plus381 Robotics KiCad plugins",
        "maintainer": {"name": "Plus381 Robotics", "contact": {"web": "https://github.com/plus381-robotics"}},
        "packages": {"url": base + "packages.json", "sha256": sha256(pkgs),
                     "update_timestamp": now, "update_time_utc": utc},
        "resources": {"url": base + "resources.zip", "sha256": sha256(res.getvalue()),
                      "update_timestamp": now, "update_time_utc": utc},
    }
    (out / "repository.json").write_text(json.dumps(repo, indent=2))
    print(f"repository: {base}repository.json")


if __name__ == "__main__":
    main()
