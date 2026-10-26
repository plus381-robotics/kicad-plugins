"""Turn a vendor download (SnapEDA, Mouser/SamacSys, UltraLibrarian,
DigiKey, LCSC, ...) into a part in the Plus381 library."""

import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .partsync import VAR, _hidden, app_dir, lib_prefix

MODEL_EXTS = (".step", ".stp", ".stpz", ".step.gz", ".stp.gz", ".wrl")

MPN_FIELDS = ("MPN", "MP", "Manufacturer_Part_Number", "Manufacturer Part Number", "PartNumber", "Part Number")
MFR_FIELDS = ("Manufacturer", "MF", "Manufacturer_Name", "Manufacturer Name", "MANUFACTURER")


# --------------------------------------------------------------------------
# S-expression helpers
# --------------------------------------------------------------------------

def block_end(text, start):
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c == '"':
            i += 1
            while text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced parentheses")


def top_level_blocks(text, keyword):
    """(start, end, name) for `(keyword "name" ...)` directly under the root list."""
    depth = 0
    i = 0
    n = len(text)
    head = re.compile(r'\(\s*' + keyword + r'\s+"((?:[^"\\]|\\.)*)"')
    out = []
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
            if depth == 2:
                m = head.match(text, i)
                if m:
                    end = block_end(text, i)
                    out.append((i, end, m.group(1)))
                    i = end
                    depth -= 1
                    continue
        elif c == ")":
            depth -= 1
        i += 1
    return out


def child_blocks(text, keyword):
    """(start, end) of `(keyword ...)` lists directly inside the list `text`."""
    out = []
    depth = 0
    i = 0
    head = re.compile(r'\(\s*' + keyword + r'[\s)]')
    while i < len(text):
        c = text[i]
        if c == '"':
            i += 1
            while text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
            if depth == 2 and head.match(text, i):
                end = block_end(text, i)
                out.append((i, end))
                i = end
                depth -= 1
                continue
        elif c == ")":
            depth -= 1
        i += 1
    return out


def q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unq(s):
    return re.sub(r"\\(.)", r"\1", s)


def get_prop(block, name):
    m = re.search(r'\(property\s+"' + re.escape(name) + r'"\s+"((?:[^"\\]|\\.)*)"', block)
    return unq(m.group(1)) if m else None


def set_prop(block, name, value, hidden=True):
    """Set a symbol property, adding it (hidden) when missing."""
    pat = re.compile(r'(\(property\s+"' + re.escape(name) + r'"\s+)"(?:[^"\\]|\\.)*"')
    if pat.search(block):
        return pat.sub(lambda m: m.group(1) + q(value), block, count=1)
    props = child_blocks(block, "property")
    at = props[-1][1] if props else block.index("\n") + 1
    new = (f'\n\t\t(property {q(name)} {q(value)}\n\t\t\t(at 0 0 0)\n'
           + ("\t\t\t(hide yes)\n" if hidden else "")
           + '\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)')
    return block[:at] + new + block[at:]


def numbers(block, key):
    m = re.search(r'\(' + key + r'\s*\(xyz\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s*\)', block)
    return [float(v) for v in m.groups()] if m else [0.0, 0.0, 0.0]


def fmt(v):
    return ("%.6f" % v).rstrip("0").rstrip(".") if v != int(v) else str(int(v))


# --------------------------------------------------------------------------
# kicad-cli
# --------------------------------------------------------------------------

def find_kicad_cli():
    exe = shutil.which("kicad-cli")
    if exe:
        return exe
    here = Path(sys.executable).resolve().parent
    candidates = [here / "kicad-cli.exe", here / "kicad-cli",
                  Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"), Path("/usr/bin/kicad-cli")]
    try:  # KiCad's bundled Python lives next to kicad-cli (Windows) / inside KiCad.app (macOS)
        base = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
        candidates[:0] = [base.parent / "kicad-cli.exe"] + \
            [p / "MacOS" / "kicad-cli" for p in base.parents if p.name == "Contents"]
    except OSError:
        pass
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidates += sorted(Path(base, "KiCad").glob("*/bin/kicad-cli.exe"), reverse=True)
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def kicad_cli(*args):
    exe = find_kicad_cli()
    if not exe:
        return False
    r = subprocess.run([exe, *[str(a) for a in args]], capture_output=True, text=True, **_hidden())
    return r.returncode == 0


# --------------------------------------------------------------------------
# Analysis of a download
# --------------------------------------------------------------------------

@dataclass
class Symbol:
    name: str
    block: str
    header: str


@dataclass
class Footprint:
    name: str
    text: str


@dataclass
class Download:
    source: Path
    tmp: Path
    symbols: list = field(default_factory=list)
    footprints: list = field(default_factory=list)
    models: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def _model_stem(p):
    low = p.name.lower()
    for ext in sorted(MODEL_EXTS, key=len, reverse=True):
        if low.endswith(ext):
            return p.name[: -len(ext)]
    return p.stem


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def analyze(source):
    source = Path(source)
    tmp = Path(tempfile.mkdtemp(prefix="plus381-"))
    dl = Download(source, tmp)
    root = tmp / "in"
    if source.is_dir():
        count = 0
        for _ in source.rglob("*"):
            count += 1
            if count > 500:
                raise ValueError(f"{source} has too many files to be a part download. "
                                 "Pick the folder that only contains the unzipped vendor files.")
        shutil.copytree(source, root)
    else:
        with zipfile.ZipFile(source) as z:
            for info in z.infolist():
                # Ignore absolute paths or '..' (zip slip).
                target = (root / info.filename).resolve()
                if root.resolve() in target.parents and not info.is_dir():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        # Some vendors put another zip inside.
        for inner in list(root.rglob("*.zip")):
            try:
                with zipfile.ZipFile(inner) as z:
                    z.extractall(inner.with_suffix(""))
            except zipfile.BadZipFile:
                pass

    files = [p for p in sorted(root.rglob("*")) if p.is_file() and "__MACOSX" not in p.parts]
    syms = [p for p in files if p.suffix.lower() == ".kicad_sym"]
    legacy = [p for p in files if p.suffix.lower() == ".lib"]
    fps = [p for p in files if p.suffix.lower() == ".kicad_mod"]
    dl.models = [p for p in files if p.name.lower().endswith(MODEL_EXTS)]

    if not syms and legacy:
        for lib in legacy:
            out = tmp / (lib.stem + "_converted.kicad_sym")
            if kicad_cli("sym", "upgrade", "--force", lib, "-o", out):
                syms.append(out)
        if not syms:
            dl.notes.append("Only an old-format (.lib) symbol was found and it could not be converted.")

    for f in syms:
        up = tmp / ("up_" + f.name)
        src = up if kicad_cli("sym", "upgrade", "--force", f, "-o", up) else f
        text = src.read_text(encoding="utf-8", errors="replace")
        blocks = top_level_blocks(text, "symbol")
        header = text[: blocks[0][0]] if blocks else ""
        for s, e, name in blocks:
            dl.symbols.append(Symbol(name, text[s:e], header))

    for f in fps:
        lib = tmp / "fp_in.pretty"
        lib.mkdir(exist_ok=True)
        shutil.copyfile(f, lib / f.name)
    if fps:
        out = tmp / "fp_up.pretty"
        lib = tmp / "fp_in.pretty"
        use = out if kicad_cli("fp", "upgrade", "--force", lib, "-o", out) else lib
        for f in sorted(use.glob("*.kicad_mod")):
            dl.footprints.append(Footprint(f.stem, f.read_text(encoding="utf-8", errors="replace")))

    if not dl.symbols:
        dl.notes.append("No KiCad symbol found in the download.")
    if not dl.footprints:
        dl.notes.append("No KiCad footprint found in the download.")
    if not dl.models:
        dl.notes.append("No 3D model found in the download.")
    return dl


def guess_model(dl, fp):
    """The model the vendor footprint refers to, else the closest name."""
    if not dl.models:
        return None
    for m in re.finditer(r'\(model\s+"((?:[^"\\]|\\.)*)"', fp.text):
        want = _norm(os.path.basename(unq(m.group(1))))
        for model in dl.models:
            if _norm(model.name) == want:
                if model.name.lower().endswith(".wrl"):
                    # A STEP next to the VRML is the better model (MCAD export).
                    for alt in dl.models:
                        if _model_stem(alt) == _model_stem(model) and not alt.name.lower().endswith(".wrl"):
                            return alt
                return model
    steps = [m for m in dl.models if not m.name.lower().endswith(".wrl")] or dl.models
    key = _norm(fp.name)
    return max(steps, key=lambda m: len(os.path.commonprefix([key, _norm(_model_stem(m))])))


def model_transform(fp):
    """(offset, rotation) of the vendor's first 3D model, to keep its placement."""
    blocks = child_blocks(fp.text, "model")
    if not blocks:
        return [0.0] * 3, [0.0] * 3
    b = fp.text[blocks[0][0]:blocks[0][1]]
    return numbers(b, "offset"), numbers(b, "rotate")


def symbol_defaults(sym):
    def first(names):
        for n in names:
            v = get_prop(sym.block, n)
            if v and v != "~":
                return v
        return ""
    ds = get_prop(sym.block, "Datasheet") or ""
    # LCSC/EasyEDA adds the Chinese name in brackets: "ST(意法半导体)" -> "ST"
    mfr = re.sub(r"\s*[(（][^)）]*[\u3000-\u9fff][^)）]*[)）]", "", first(MFR_FIELDS)).strip()
    return {
        "mpn": first(MPN_FIELDS) or sym.name,
        "manufacturer": mfr,
        "datasheet": "" if ds == "~" else ds,
    }


# --------------------------------------------------------------------------
# Writing into the library
# --------------------------------------------------------------------------

def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return re.sub(r"\s+", "_", name)


def categories(parts_dir):
    data = json.loads((Path(parts_dir) / "categories.json").read_text(encoding="utf-8"))
    return data["categories"]


def sym_file(parts_dir, category, name):
    return Path(parts_dir) / "symbols" / f"{lib_prefix(parts_dir)}{category}.kicad_symdir" / f"{name}.kicad_sym"


def fp_file(parts_dir, category, name):
    return Path(parts_dir) / "footprints" / f"{lib_prefix(parts_dir)}{category}.pretty" / f"{name}.kicad_mod"


def find_existing(parts_dir, kind, name):
    """Library nicknames that already hold a symbol/footprint with this name."""
    pattern = f"symbols/*.kicad_symdir/{name}.kicad_sym" if kind == "sym" else f"footprints/*.pretty/{name}.kicad_mod"
    return [p.parent.stem for p in Path(parts_dir).glob(pattern)]


def find_same_model(parts_dir, model):
    d = Path(parts_dir) / "3dmodels"
    if not d.is_dir():
        return None
    size = model.stat().st_size
    for p in sorted(d.iterdir()):
        if p.is_file() and p.stat().st_size == size and filecmp.cmp(p, model, shallow=False):
            return p.name
    return None


def install_model(parts_dir, model, fp_name):
    """Copy the model into 3dmodels/ (or reuse an identical one). Returns file name."""
    same = find_same_model(parts_dir, model)
    if same:
        return same
    d = Path(parts_dir) / "3dmodels"
    d.mkdir(exist_ok=True)
    ext = model.name[len(_model_stem(model)):].lower()
    base = safe_name(_model_stem(model))
    name = base + ext
    n = 2
    while (d / name).exists():  # different content, same name
        name = f"{base}_{n}{ext}"
        n += 1
    shutil.copyfile(model, d / name)
    return name


def build_footprint(fp, name, model_name, offset, rotation, model_path=None):
    """The footprint as it will be saved. model_path overrides the model
    reference (the preview uses the file's real location)."""
    text = fp.text.lstrip()
    text = re.sub(r'^\((?:footprint|module)\s+"?(?:[^"\\\s]|\\.)*"?', lambda _: "(footprint " + q(name), text, count=1)
    for s, e in reversed(child_blocks(text, "model")):
        line_start = text.rfind("\n", 0, s) + 1
        text = text[:line_start] + text[e:].lstrip(" \t").lstrip("\n")
    if model_name or model_path:
        ref = model_path or "${%s}/3dmodels/%s" % (VAR, model_name)
        model = (f"\t(model {q(ref)}\n"
                 f"\t\t(offset\n\t\t\t(xyz {' '.join(fmt(v) for v in offset)})\n\t\t)\n"
                 f"\t\t(scale\n\t\t\t(xyz 1 1 1)\n\t\t)\n"
                 f"\t\t(rotate\n\t\t\t(xyz {' '.join(fmt(v) for v in rotation)})\n\t\t)\n\t)\n")
        close = text.rstrip().rfind(")")
        text = text[:close].rstrip("\n\t ") + "\n" + model + text[close:]
    return text.rstrip() + "\n"


def build_symbol(sym, name, fp_ref, mpn, manufacturer, datasheet):
    block = sym.block
    if sym.name != name:
        # Rename the symbol and its unit sub-symbols (NAME_0_1, NAME_1_1, ...).
        block = block.replace(f'(symbol {q(sym.name)}', f'(symbol {q(name)}', 1)
        block = re.sub(r'\(symbol\s+"' + re.escape(sym.name) + r'(_\d+_\d+)"',
                       lambda m: f'(symbol "{name}{m.group(1)}"', block)
        if get_prop(block, "Value") == sym.name:
            block = set_prop(block, "Value", name, hidden=False)
    block = set_prop(block, "Footprint", fp_ref)
    block = set_prop(block, "Datasheet", datasheet or "")
    if mpn:
        block = set_prop(block, "MPN", mpn)
    if manufacturer:
        block = set_prop(block, "Manufacturer", manufacturer)
    header = sym.header or '(kicad_symbol_lib\n\t(version 20251024)\n\t(generator "plus381_tools")\n'
    return header.rstrip() + "\n\t" + block.strip() + "\n)\n"


@dataclass
class Choices:
    category: str
    symbol: object = None          # Symbol or None
    symbol_name: str = ""
    footprint: object = None       # Footprint or None
    footprint_name: str = ""
    model: object = None           # Path or None
    offset: list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rotation: list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    mpn: str = ""
    manufacturer: str = ""
    datasheet: str = ""
    replace: bool = False


def check_choices(parts_dir, c):
    """Problems that stop the import (list of strings)."""
    errors = []
    if c.symbol and re.search(r'\(extends\s', c.symbol.block):
        errors.append("This symbol is derived from another one (extends), which isn't supported yet.")
    if c.symbol and not c.symbol_name:
        errors.append("Symbol name is empty.")
    if c.footprint and not c.footprint_name:
        errors.append("Footprint name is empty.")
    if not c.replace:
        if c.symbol:
            for lib in find_existing(parts_dir, "sym", c.symbol_name):
                errors.append(f"Symbol {lib}:{c.symbol_name} already exists (tick 'Replace existing').")
        if c.footprint:
            for lib in find_existing(parts_dir, "fp", c.footprint_name):
                errors.append(f"Footprint {lib}:{c.footprint_name} already exists (tick 'Replace existing').")
    return errors


def do_import(parts_dir, c):
    """Write the part. Returns a list of (what, path) that were written."""
    written = []
    nick = lib_prefix(parts_dir) + c.category
    fp_ref = ""
    if c.footprint:
        model_name = install_model(parts_dir, c.model, c.footprint_name) if c.model else None
        if model_name:
            written.append(("3D model", Path(parts_dir) / "3dmodels" / model_name))
        # A replaced part may live in another category: remove the old copy.
        if c.replace:
            for lib in find_existing(parts_dir, "fp", c.footprint_name):
                if lib != nick:
                    (Path(parts_dir) / "footprints" / f"{lib}.pretty" / f"{c.footprint_name}.kicad_mod").unlink()
        dest = fp_file(parts_dir, c.category, c.footprint_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(build_footprint(c.footprint, c.footprint_name, model_name, c.offset, c.rotation),
                        encoding="utf-8")
        _normalise_footprint(dest)
        written.append(("footprint", dest))
        fp_ref = f"{nick}:{c.footprint_name}"
    elif c.symbol:
        fp_ref = get_prop(c.symbol.block, "Footprint") or ""
    if c.symbol:
        if c.replace:
            for lib in find_existing(parts_dir, "sym", c.symbol_name):
                if lib != nick:
                    (Path(parts_dir) / "symbols" / f"{lib}.kicad_symdir" / f"{c.symbol_name}.kicad_sym").unlink()
        dest = sym_file(parts_dir, c.category, c.symbol_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        backup = dest.read_text(encoding="utf-8") if dest.exists() else None
        dest.write_text(build_symbol(c.symbol, c.symbol_name, fp_ref, c.mpn, c.manufacturer, c.datasheet),
                        encoding="utf-8")
        if find_kicad_cli() and not kicad_cli("sym", "upgrade", "--force", dest):
            if backup is None:
                dest.unlink()
            else:
                dest.write_text(backup, encoding="utf-8")
            raise RuntimeError("KiCad could not read the converted symbol; nothing was saved for it.")
        written.append(("symbol", dest))
    return written


def _normalise_footprint(path):
    """Let KiCad re-save the footprint so it looks exactly like one saved from
    the Footprint Editor (keeps future diffs small)."""
    if not find_kicad_cli():
        return
    tmp = Path(tempfile.mkdtemp(prefix="plus381-fp-"))
    try:
        src = tmp / "in.pretty"
        src.mkdir()
        shutil.copyfile(path, src / path.name)
        if kicad_cli("fp", "upgrade", "--force", src, "-o", tmp / "out.pretty"):
            out = tmp / "out.pretty" / path.name
            if out.exists():
                shutil.copyfile(out, path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 3D preview
# --------------------------------------------------------------------------

PREVIEW_VIEWS = {  # name: kicad-cli render arguments
    "3D": ["--side", "top", "--rotate", "-45,0,45"],
    "Front": ["--side", "front"],
    "Right": ["--side", "right"],
    "Top": ["--side", "top"],
}


def _bbox(text):
    """Rough extent of a footprint (pads and graphics, not texts) in mm."""
    xs, ys = [], []
    for m in re.finditer(r'\((?:start|end|center|mid|xy)\s+([-\d.]+)\s+([-\d.]+)\)', text):
        xs.append(float(m.group(1)))
        ys.append(float(m.group(2)))
    for pad in child_blocks(text, "pad"):
        b = text[pad[0]:pad[1]]
        at = re.search(r'\(at\s+([-\d.]+)\s+([-\d.]+)', b)
        size = re.search(r'\(size\s+([-\d.]+)\s+([-\d.]+)\)', b)
        if at:
            x, y = float(at.group(1)), float(at.group(2))
            w, h = (float(size.group(1)), float(size.group(2))) if size else (0, 0)
            r = max(w, h) / 2
            xs += [x - r, x + r]
            ys += [y - r, y + r]
    if not xs:
        return -2.5, -2.5, 2.5, 2.5
    return min(xs), min(ys), max(xs), max(ys)


_EXTENT_CACHE = {}


def model_box(path):
    """Bounding box ((xmin, ymin, zmin), (xmax, ymax, zmax)) of a 3D model in
    mm, read from the STEP/VRML points. None if the file can't be read."""
    path = Path(path)
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return None
    if key in _EXTENT_CACHE:
        return _EXTENT_CACHE[key]
    try:
        raw = path.read_bytes()
        if raw[:2] == b"\x1f\x8b":  # .step.gz / .stpz / .wrl.gz
            import gzip
            raw = gzip.decompress(raw)
        text = raw.decode("latin-1")
    except (OSError, EOFError):
        return None
    num = r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
    if "#VRML" in text[:20]:
        scale = 2.54  # KiCad VRML models: 1 unit = 0.1 inch
        pts = re.findall(num + r"\s+" + num + r"\s+" + num,
                         " ".join(re.findall(r"point\s*\[([^\]]*)\]", text)))
    else:
        if re.search(r"CONVERSION_BASED_UNIT\s*\(\s*'INCH'", text, re.I):
            scale = 25.4
        elif re.search(r"SI_UNIT\s*\(\s*\.MILLI\.\s*,\s*\.METRE\.", text, re.I):
            scale = 1.0
        elif re.search(r"SI_UNIT\s*\(\s*\$\s*,\s*\.METRE\.", text, re.I):
            scale = 1000.0
        else:
            scale = 1.0
        points = dict((m.group(1), m.groups()[1:]) for m in re.finditer(
            r"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*" + num + r"\s*,\s*" + num + r"\s*,\s*" + num,
            text))
        # Only the shape's corners (vertices): files also hold construction
        # points (axis placements, spline controls) far away from the part.
        pts = [points[v] for v in re.findall(r"VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#(\d+)", text) if v in points]
        pts = pts or list(points.values())
    if not pts:
        _EXTENT_CACHE[key] = None
        return None
    xs, ys, zs = ([float(p[i]) * scale for p in pts] for i in range(3))
    box = ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))
    _EXTENT_CACHE[key] = box
    return box


def placed_box(box, offset, rotation):
    """Where the model ends up on the board: KiCad rotates the model about X,
    then Y, then Z (angles negated), then moves it by the offset.
    Returns board coordinates (x, y down like the footprint) and height:
    (x0, y0, x1, y1, z0, z1) in mm."""
    import math
    (a0, b0, c0), (a1, b1, c1) = box
    corners = [(x, y, z) for x in (a0, a1) for y in (b0, b1) for z in (c0, c1)]
    rx, ry, rz = (math.radians(-v) for v in rotation)
    out = []
    for x, y, z in corners:
        y, z = y * math.cos(rx) - z * math.sin(rx), y * math.sin(rx) + z * math.cos(rx)
        x, z = x * math.cos(ry) + z * math.sin(ry), -x * math.sin(ry) + z * math.cos(ry)
        x, y = x * math.cos(rz) - y * math.sin(rz), x * math.sin(rz) + y * math.cos(rz)
        out.append((x + offset[0], -(y + offset[1]), z + offset[2]))  # 3D y is up, board y is down
    xs, ys, zs = zip(*out)
    return min(xs), min(ys), max(xs), max(ys), min(zs), max(zs)


def model_reach(fp, model, offset, rotation):
    """Where the 3D model sits: (x0, y0, x1, y1, z0, z1) in board mm.

    Uses the model's box turned like KiCad turns it. Some STEP files store
    the shape turned and fix it up with transforms this simple reader can't
    follow; then the turned box doesn't match the footprint and a cautious
    box is used instead (a cube as big as the model's longest side), so the
    part always fits, maybe a little smaller in the picture."""
    box = model_box(model)
    if not box:
        return None
    placed = placed_box(box, offset, rotation)
    fx0, fy0, fx1, fy1 = _bbox(fp.text)
    fw, fh = fx1 - fx0 + 2, fy1 - fy0 + 2
    pw, ph = placed[2] - placed[0], placed[3] - placed[1]
    if 0.4 * fw <= pw <= 2.5 * fw and 0.4 * fh <= ph <= 2.5 * fh:
        return placed
    d = max(b - a for a, b in zip(*box))
    return (offset[0] - d / 2, -offset[1] - d / 2, offset[0] + d / 2, -offset[1] + d / 2, -d / 4, d)


def preview_board(fp_text, path, model=None, min_size=0.0):
    """A tiny board holding only this footprint, to render it in 3D.
    kicad-cli zooms to fit the board, so the board is made to cover the
    footprint, the placed 3D model (x0, y0, x1, y1) and at least min_size
    mm, which keeps tall models in view."""
    text = fp_text.strip()
    # Hide reference/value texts: they only clutter the preview.
    text = re.sub(r'(\(property\s+"(?:Reference|Value)"\s+"(?:[^"\\]|\\.)*")', r'\1 (hide yes)', text)
    body = text[text.index("\n") + 1:text.rfind(")")]
    x0, y0, x1, y1 = _bbox(text)
    if model:
        x0, y0, x1, y1 = min(x0, model[0]), min(y0, model[1]), max(x1, model[2]), max(y1, model[3])
    grow = max(0.0, min_size - (x1 - x0)) / 2, max(0.0, min_size - (y1 - y0)) / 2
    x0, x1, y0, y1 = x0 - grow[0], x1 + grow[0], y0 - grow[1], y1 + grow[1]
    margin = max(1.0, 0.15 * max(x1 - x0, y1 - y0))
    cx, cy = 100, 100
    path.write_text(
        '(kicad_pcb (version 20260206) (generator "plus381_preview") (generator_version "10.0")\n'
        ' (general (thickness 1.6)) (paper "A4")\n'
        ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (1 "F.Mask" user) (3 "B.Mask" user)'
        ' (5 "F.SilkS" user) (7 "B.SilkS" user) (13 "F.Paste" user) (15 "B.Paste" user)'
        ' (25 "Edge.Cuts" user) (31 "F.CrtYd" user) (35 "F.Fab" user))\n'
        f' (footprint "preview:part" (layer "F.Cu") (at {cx} {cy})\n{body})\n'
        f' (gr_rect (start {cx + x0 - margin:.3f} {cy + y0 - margin:.3f}) (end {cx + x1 + margin:.3f} {cy + y1 + margin:.3f})'
        ' (stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))\n)\n', encoding="utf-8")


def _preview_env():
    """kicad-cli takes 3D viewer options from the user's KiCad settings (for
    example "show through-hole models" may be off). Give it a private settings
    folder where every kind of part is shown, without touching the user's."""
    base = app_dir() / "preview-kicad-config"
    for d in [base / "10.0"] + [base / p.name for p in partsync_config_dirs()]:
        d.mkdir(parents=True, exist_ok=True)
        f = d / "3d_viewer.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        render = data.setdefault("render", {})
        for key in ("show_footprints_normal", "show_footprints_insert", "show_footprints_virtual",
                    "show_footprints_not_in_posfile", "show_footprints_dnp", "show_board_body"):
            render[key] = True
        render["show_fp_references"] = render["show_fp_values"] = render["show_fp_text"] = False
        f.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return dict(os.environ, KICAD_CONFIG_HOME=str(base))


def partsync_config_dirs():
    from .partsync import kicad_config_dirs
    return kicad_config_dirs()


def render_preview(fp, model, offset, rotation, outdir, size=(260, 200), views=None, on_view=None):
    """Render the footprint with its 3D model from several sides using
    kicad-cli. Calls on_view(name, png_path) as each view finishes."""
    cli = find_kicad_cli()
    if not cli:
        raise RuntimeError("kicad-cli not found, so no 3D preview.")
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    text = build_footprint(fp, "preview", None, offset, rotation,
                           model_path=Path(model).resolve().as_posix() if model else None)
    # Two boards: one sized for the views from above, one also tall enough
    # for the side views (kicad-cli fits the board's width into the picture).
    placed = model_reach(fp, model, offset, rotation) if model else None
    height = max(placed[5], -placed[4]) if placed else 0.0
    boards = {}  # kicad-cli fits the board, so each kind of view gets a board big enough for it
    for kind, factor in (("top", 0.0), ("3d", 1.5), ("side", 2.0)):
        boards[kind] = outdir / f"preview-{kind}.kicad_pcb"
        preview_board(text, boards[kind], placed[:4] if placed else None, min_size=factor * height)
    results = {}
    env = _preview_env()
    for name in views or PREVIEW_VIEWS:
        png = outdir / f"{name}.png"
        if png.exists():
            png.unlink()
        source = boards["side" if name in ("Front", "Right") else "3d" if name == "3D" else "top"]
        r = subprocess.run([cli, "pcb", "render", str(source), "-o", str(png), "-w", str(size[0]),
                            "-h", str(size[1]), "--background", "opaque", "--zoom", "0.9",
                            *PREVIEW_VIEWS[name]], capture_output=True, text=True, env=env, **_hidden())
        if r.returncode == 0 and png.exists():
            results[name] = png
            if on_view:
                on_view(name, png)
    return results


# --------------------------------------------------------------------------
# Parts by number
# --------------------------------------------------------------------------

LCSC_RE = re.compile(r"^C\d{1,9}$", re.I)


def fetch_lcsc(lcsc_id, outdir):
    """Download symbol, footprint and 3D model for an LCSC part number (e.g.
    C8734) with easyeda2kicad. Returns the folder to give to analyze()."""
    lcsc_id = lcsc_id.strip().upper()
    if not LCSC_RE.match(lcsc_id):
        raise ValueError(f"'{lcsc_id}' is not an LCSC part number (they look like C8734).")
    out = Path(outdir) / lcsc_id
    out.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, "-m", "easyeda2kicad", "--full", "--lcsc_id", lcsc_id,
                        "--output", str(out / "lcsc.kicad_sym")], cwd=str(out), capture_output=True,
                       text=True, timeout=180, **_hidden())
    text = (r.stdout + r.stderr)
    if "No module named easyeda2kicad" in text:
        raise RuntimeError("The LCSC converter (easyeda2kicad) isn't installed. Update Plus381 Tools in the "
                           "Plugin and Content Manager and restart KiCad (it needs internet the first time).")
    if not any(out.rglob("*.kicad_mod")) and not any(out.rglob("*.kicad_sym")):
        errors = [l for l in text.splitlines() if "ERROR" in l] or text.strip().splitlines()[-3:]
        raise RuntimeError(f"LCSC/EasyEDA has no KiCad data for {lcsc_id}:\n" + "\n".join(errors))
    return out


def search_urls(query):
    from urllib.parse import quote_plus
    q = quote_plus(query.strip())
    return {"lcsc": f"https://www.lcsc.com/search?q={q}",
            "digikey": f"https://www.digikey.com/en/products/result?keywords={q}"}


def _pad_svg(svg, frac=0.08):
    """Widen an SVG's viewBox a little so the drawing doesn't touch the edges."""
    text = Path(svg).read_text(encoding="utf-8")
    m = re.search(r'viewBox="([-\d.e]+)[ ,]+([-\d.e]+)[ ,]+([-\d.e]+)[ ,]+([-\d.e]+)"', text)
    if m:
        x, y, w, h = (float(v) for v in m.groups())
        d = frac * max(w, h)
        text = text.replace(m.group(0), f'viewBox="{x - d} {y - d} {w + 2 * d} {h + 2 * d}"', 1)
        Path(svg).write_text(text, encoding="utf-8")
    return Path(svg)


def render_symbol_svg(sym, outdir):
    """The symbol as an SVG drawing (kicad-cli), or None."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    lib = outdir / "symbol.kicad_sym"
    header = sym.header or '(kicad_symbol_lib\n\t(version 20251024)\n\t(generator "plus381_tools")\n'
    lib.write_text(header.rstrip() + "\n\t" + sym.block.strip() + "\n)\n", encoding="utf-8")
    svgdir = outdir / "symbol-svg"
    svgdir.mkdir(exist_ok=True)
    if not kicad_cli("sym", "export", "svg", lib, "--symbol", sym.name, "-o", svgdir):
        return None
    svgs = sorted(svgdir.glob("*.svg"))
    return _pad_svg(svgs[0]) if svgs else None


def render_footprint_svg(fp, outdir):
    """Pads, silkscreen and courtyard as an SVG drawing (kicad-cli), or None."""
    outdir = Path(outdir)
    lib = outdir / "fp.pretty"
    lib.mkdir(parents=True, exist_ok=True)
    text = re.sub(r'(\(property\s+"(?:Reference|Value)"\s+"(?:[^"\\]|\\.)*")', r'\1 (hide yes)', fp.text)
    text = re.sub(r'^\((?:footprint|module)\s+"?(?:[^"\\\s]|\\.)*"?', '(footprint "preview"', text.lstrip(), count=1)
    (lib / "preview.kicad_mod").write_text(text, encoding="utf-8")
    svgdir = outdir / "fp-svg"
    svgdir.mkdir(exist_ok=True)
    if not kicad_cli("fp", "export", "svg", lib, "--footprint", "preview",
                     "--layers", "F.Cu,B.Cu,F.SilkS,F.CrtYd,Edge.Cuts", "-o", svgdir):
        return None
    svgs = sorted(svgdir.glob("*.svg"))
    return _pad_svg(svgs[0]) if svgs else None
