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

from .partsync import LIB_PREFIX, VAR, _hidden

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
    return {
        "mpn": first(MPN_FIELDS) or sym.name,
        "manufacturer": first(MFR_FIELDS),
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
    return Path(parts_dir) / "symbols" / f"{LIB_PREFIX}{category}.kicad_symdir" / f"{name}.kicad_sym"


def fp_file(parts_dir, category, name):
    return Path(parts_dir) / "footprints" / f"{LIB_PREFIX}{category}.pretty" / f"{name}.kicad_mod"


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
    nick = LIB_PREFIX + c.category
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


def preview_board(fp_text, path):
    """A tiny board holding only this footprint, to render it in 3D."""
    text = fp_text.strip()
    # Hide reference/value texts: they only clutter the preview.
    text = re.sub(r'(\(property\s+"(?:Reference|Value)"\s+"(?:[^"\\]|\\.)*")', r'\1 (hide yes)', text)
    body = text[text.index("\n") + 1:text.rfind(")")]
    x0, y0, x1, y1 = _bbox(text)
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
    board = outdir / "preview.kicad_pcb"
    preview_board(text, board)
    results = {}
    for name in views or PREVIEW_VIEWS:
        png = outdir / f"{name}.png"
        if png.exists():
            png.unlink()
        r = subprocess.run([cli, "pcb", "render", str(board), "-o", str(png), "-w", str(size[0]),
                            "-h", str(size[1]), "--background", "opaque", "--zoom", "0.9",
                            *PREVIEW_VIEWS[name]], capture_output=True, text=True, **_hidden())
        if r.returncode == 0 and png.exists():
            results[name] = png
            if on_view:
                on_view(name, png)
    return results
