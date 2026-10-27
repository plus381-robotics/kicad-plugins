"""wxPython windows for the Plus381 Tools plugin."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import webbrowser
from pathlib import Path

import wx
import wx.adv
import wx.svg

from . import autosync, github, importer, partsync, publish
from .partsync import Conflict, GitError

TITLE = "Plus381 Tools"
PAD = 8
ICON_DIR = Path(__file__).resolve().parent.parent / "icons"
CURRENT_ICON = "status"  # icon for windows opened without a parent


def set_icon(win, kind=None):
    """Give a window its own taskbar/title-bar icon (otherwise Linux shows a
    generic Python icon and all Plus381 windows look alike)."""
    kind = kind or CURRENT_ICON
    try:
        bundle = wx.IconBundle()
        for size in (16, 24, 32, 48, 64, 128):
            f = ICON_DIR / f"window-{kind}-{size}.png"
            if f.exists():
                bundle.AddIcon(wx.Icon(str(f), wx.BITMAP_TYPE_PNG))
        win.SetIcons(bundle)
        win._icon_kind = kind
    except Exception:  # noqa: BLE001 - an icon is never worth failing for
        pass


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def run_busy(parent, message, fn, *args, **kwargs):
    """Run fn in a thread while showing a pulsing progress window.
    Returns fn's result or re-raises its exception."""
    result = {}

    def work():
        try:
            result["value"] = fn(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001 - re-raised in the UI thread
            result["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    dlg = wx.ProgressDialog(TITLE, message, parent=parent, style=wx.PD_APP_MODAL | wx.PD_SMOOTH)
    set_icon(dlg, getattr(parent, "_icon_kind", None))
    while t.is_alive():
        dlg.Pulse()
        wx.MilliSleep(80)
        wx.Yield()
    dlg.Destroy()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _message(parent, text, title, style):
    dlg = wx.MessageDialog(parent, str(text), f"{TITLE}: {title}", style)
    set_icon(dlg, getattr(parent, "_icon_kind", None))
    answer = dlg.ShowModal()
    dlg.Destroy()
    return answer


def error(parent, text, title="Something went wrong"):
    _message(parent, text, title, wx.OK | wx.ICON_ERROR)


def info(parent, text, title="Done"):
    _message(parent, text, title, wx.OK | wx.ICON_INFORMATION)


def label(parent, text, bold=False, wrap=0, colour=None):
    st = wx.StaticText(parent, label=text)
    if bold:
        st.SetFont(st.GetFont().Bold())
    if wrap:
        st.Wrap(wrap)
    if colour:
        st.SetForegroundColour(colour)
    return st


def quick_sync(parent, cfg):
    """Get the newest parts before doing anything. Failing to sync (offline)
    is not fatal: the user can still work on their local copy."""
    try:
        return run_busy(parent, "Getting the newest parts from the team...", partsync.run_sync, cfg, True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)}


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

def _same_repo(a, b):
    def norm(u):
        u = u.strip().lower().rstrip("/")
        u = u[:-4] if u.endswith(".git") else u
        return u.replace("git@github.com:", "https://github.com/")
    return norm(a) == norm(b)


def do_setup(log, parts_dir, name, email, auto, repo=None):
    parts_dir = Path(parts_dir).expanduser()
    repo = (repo or "").strip() or partsync.REPO_URL
    gitexe = partsync.find_git()
    if not gitexe:
        raise GitError("Git was not found.\n\nWindows: install Git for Windows (git-scm.com) or GitHub Desktop.\n"
                       "macOS: install Xcode Command Line Tools or 'brew install git git-lfs'.\n"
                       "Linux: install git and git-lfs with your package manager.")
    log(f"Git: {gitexe}")
    hidden = partsync._hidden()
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    r = subprocess.run([gitexe, "lfs", "version"], capture_output=True, text=True, **hidden)
    if r.returncode != 0:
        raise GitError("Git LFS is missing (needed for 3D models).\n\nmacOS: brew install git-lfs\n"
                       "Linux: install the git-lfs package\nWindows: reinstall Git for Windows with LFS ticked.")
    subprocess.run([gitexe, "lfs", "install", "--skip-repo"], capture_output=True, **hidden)
    log("Git LFS: ok")

    if (parts_dir / ".git").exists():
        url = subprocess.run([gitexe, "-C", str(parts_dir), "remote", "get-url", "origin"],
                             capture_output=True, text=True, **hidden).stdout.strip()
        if not _same_repo(url, repo):
            raise GitError(f"{parts_dir} holds a different library ({url}).\n\nTo switch to {repo}, choose a "
                           "new, empty library folder.")
        log(f"Using existing library at {parts_dir}")
    else:
        if parts_dir.exists() and any(parts_dir.iterdir()):
            raise GitError(f"{parts_dir} is not empty. Choose an empty or new folder.")
        parts_dir.parent.mkdir(parents=True, exist_ok=True)
        log(f"Downloading {repo} to {parts_dir} ...")
        r = subprocess.run([gitexe, "clone", repo, str(parts_dir)], capture_output=True,
                           text=True, env=env, **hidden)
        if r.returncode != 0 or not (parts_dir / "categories.json").exists():
            shutil.rmtree(parts_dir, ignore_errors=True)  # don't leave a half download behind
            if r.returncode == 0:
                raise GitError(f"{repo} is not a parts library: it has no categories.json. Start your own "
                               "library from a copy of github.com/plus381-robotics/kicad-parts.")
            raise GitError("Could not download the parts library:\n" + (r.stderr or r.stdout).strip() +
                           "\n\nCheck the repository address and your internet connection.")
        log("Download: ok")

    cfg = partsync.load_config()
    if not _same_repo(cfg.get("repo_url") or partsync.REPO_URL, repo):
        # Another library: the GitHub login and publish mode were for the old one.
        cfg.pop("publish_mode", None)
    cfg.update(parts_dir=str(parts_dir), git=gitexe, repo_url=repo)
    partsync.save_config(cfg)
    if name and email:
        publish.set_identity(cfg, name, email)
        log(f"Commits will be signed as {name} <{email}>")

    problems = partsync.ensure_lib_tables(cfg["parts_dir"])
    for p in problems:
        log("! " + p)
    if not problems:
        log(f"Registered {len(partsync.libraries(cfg['parts_dir']))} libraries in KiCad")

    state = partsync.ensure_env_var(cfg["parts_dir"])
    if state == "pending":
        autosync.start_configure_after_exit()
        log(f"Path variable {partsync.VAR} will be saved as soon as KiCad is closed")
    elif state == "ok":
        log(f"Path variable {partsync.VAR} = {parts_dir.as_posix()}")
    else:
        log("! " + state)

    try:
        if auto:
            log("Background sync: " + autosync.install())
        else:
            autosync.uninstall()
            log("Background sync: off")
    except Exception as e:  # noqa: BLE001 - the library still works without it
        problems.append(f"Background sync could not be turned on ({e}). The library still updates "
                        "every time you use a Plus381 button.")
        log("! " + problems[-1])
    return cfg, problems


class SetupDialog(wx.Dialog):
    def __init__(self, parent=None):
        super().__init__(parent, title=f"{TITLE}: Setup", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        set_icon(self, "setup")
        cfg = partsync.load_config()
        p = wx.Panel(self)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(label(p, "Connect this computer to the parts library", bold=True), 0, wx.ALL, PAD)
        s.Add(label(p, "This downloads the parts library, adds its libraries to KiCad and keeps them up to "
                       "date in the background. You only do this once per computer.", wrap=520),
              0, wx.LEFT | wx.RIGHT, PAD)

        g = wx.FlexGridSizer(2, PAD, PAD)
        g.AddGrowableCol(1)
        g.Add(label(p, "Library repository:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.repo = wx.TextCtrl(p, value=partsync.repo_url(cfg))
        self.repo.SetToolTip("The GitHub repository holding the parts. Leave the default for the Plus381 team "
                             "library; other teams can use their own copy of kicad-parts.")
        g.Add(self.repo, 1, wx.EXPAND)
        g.Add(label(p, "Library folder:"), 0, wx.ALIGN_CENTER_VERTICAL)
        # A plain text field: wx.DirPickerCtrl clears paths that don't exist yet.
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.folder = wx.TextCtrl(p, value=cfg.get("parts_dir") or str(partsync.default_parts_dir()))
        browse = wx.Button(p, label="Browse...")
        browse.Bind(wx.EVT_BUTTON, self.on_browse)
        row.Add(self.folder, 1, wx.EXPAND | wx.RIGHT, PAD)
        row.Add(browse)
        g.Add(row, 1, wx.EXPAND)
        name = email = ""
        gitexe = partsync.find_git()
        if gitexe:
            r = lambda k: subprocess.run([gitexe, "config", "--global", k], capture_output=True, text=True,
                                         **partsync._hidden()).stdout.strip()
            name, email = r("user.name"), r("user.email")
        g.Add(label(p, "Your name:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.name = wx.TextCtrl(p, value=name)
        g.Add(self.name, 1, wx.EXPAND)
        g.Add(label(p, "GitHub e-mail:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.email = wx.TextCtrl(p, value=email)
        g.Add(self.email, 1, wx.EXPAND)
        s.Add(g, 0, wx.EXPAND | wx.ALL, PAD)
        s.Add(label(p, "Name and e-mail are optional: they are filled in from your GitHub account when you "
                       "first publish.", wrap=520), 0, wx.LEFT | wx.RIGHT, PAD)

        self.auto = wx.CheckBox(p, label="Keep the library up to date automatically while KiCad is open "
                                         "(checks every minute)")
        self.auto.SetValue(True)
        s.Add(self.auto, 0, wx.ALL, PAD)

        self.log = wx.TextCtrl(p, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(560, 160))
        s.Add(self.log, 1, wx.EXPAND | wx.ALL, PAD)

        b = wx.BoxSizer(wx.HORIZONTAL)
        self.go = wx.Button(p, label="Set up")
        self.close = wx.Button(p, wx.ID_CANCEL, label="Close")
        b.Add(self.go, 0, wx.RIGHT, PAD)
        b.Add(self.close)
        s.Add(b, 0, wx.ALIGN_RIGHT | wx.ALL, PAD)
        p.SetSizer(s)
        s.Fit(self)
        self.go.Bind(wx.EVT_BUTTON, self.on_go)
        self.done = False

    def on_browse(self, _):
        with wx.DirDialog(self, "Folder for the parts library (a new, empty folder)",
                          defaultPath=str(Path(self.folder.GetValue()).parent)) as d:
            if d.ShowModal() == wx.ID_OK:
                self.folder.SetValue(str(Path(d.GetPath()) / "plus381-parts"))

    def append(self, text):
        wx.CallAfter(self.log.AppendText, text + "\n")

    def on_go(self, _):
        self.go.Disable()
        try:
            _, problems = run_busy(self, "Setting up (the first download can take a few minutes)...", do_setup,
                                   self.append, self.folder.GetValue().strip(), self.name.GetValue().strip(),
                                   self.email.GetValue().strip(), self.auto.GetValue(), self.repo.GetValue())
        except Exception as e:  # noqa: BLE001
            self.append("ERROR: " + str(e))
            error(self, e, "Setup failed")
            self.go.Enable()
            return
        self.done = True
        self.close.SetLabel("Finish")
        msg = ("Setup finished.\n\nClose KiCad completely (all windows) and open it again. "
               "After that the Plus381 libraries are available in every project.")
        if problems:
            msg += "\n\nSome things still need attention:\n- " + "\n- ".join(problems)
        info(self, msg, "Setup finished")


def ensure_setup(parent=None):
    cfg = partsync.load_config()
    if partsync.is_repo(cfg):
        return cfg
    dlg = SetupDialog(parent)
    dlg.ShowModal()
    done = dlg.done
    dlg.Destroy()
    cfg = partsync.load_config()
    return cfg if done and partsync.is_repo(cfg) else None


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

def svg_on_white(data, w, h):
    """An SVG drawing centred on a white w x h picture, without transparency.
    (Drawing onto a white bitmap with a GraphicsContext gives a black
    background when the plugin runs inside the PCB editor.)"""
    img = wx.svg.SVGimage.CreateFromBytes(data)
    scale = min(w / img.width, h / img.height)
    sw, sh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
    drawn = img.ConvertToScaledBitmap(wx.Size(sw, sh)).ConvertToImage()
    rgb, alpha = bytes(drawn.GetData()), drawn.GetAlpha() if drawn.HasAlpha() else None
    out = bytearray(b"\xff" * (w * h * 3))
    x0, y0 = (w - sw) // 2, (h - sh) // 2
    for y in range(sh):
        for x in range(sw):
            k = y * sw + x
            a = alpha[k] if alpha is not None else 255
            if a:
                o = ((y0 + y) * w + x0 + x) * 3
                for c in range(3):
                    out[o + c] = (rgb[3 * k + c] * a + 255 * (255 - a)) // 255
    return wx.Image(w, h, bytes(out)).ConvertToBitmap()


class CategoryCombo(wx.adv.OwnerDrawnComboBox):
    """Category drop-down whose list shows each category's description under
    its name, so you can see what belongs where while choosing."""

    def __init__(self, parent, cats):
        self.cats = cats
        super().__init__(parent, choices=list(cats), style=wx.CB_READONLY, size=(170, -1))
        self.SetPopupMinWidth(560)

    def OnDrawItem(self, dc, rect, item, flags):
        if item == wx.NOT_FOUND:
            return
        name = self.GetString(item)
        if flags & wx.adv.ODCB_PAINTING_CONTROL:  # the closed control: name only
            dc.DrawText(name, rect.x + 4, rect.y + (rect.height - dc.GetCharHeight()) // 2)
            return
        font = self.GetFont()
        dc.SetFont(font.Bold())
        dc.DrawText(name, rect.x + 6, rect.y + 3)
        dc.SetFont(font.Smaller())
        if not flags & wx.adv.ODCB_PAINTING_SELECTED:
            dc.SetTextForeground(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        dc.DrawText(self.cats.get(name, ""), rect.x + 6, rect.y + 3 + dc.GetCharHeight() + 4)

    def OnMeasureItem(self, item):
        return self.GetCharHeight() * 2 + 12

    def OnMeasureItemWidth(self, item):
        return 560


class ImportDialog(wx.Dialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent, title=f"{TITLE}: Import part",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        set_icon(self, "import")
        self.cfg = cfg
        self.dl = None
        self.cats = importer.categories(cfg["parts_dir"])
        self.known_libs = set(partsync.libraries(cfg["parts_dir"]))  # libraries that existed before this import
        p = self.panel = wx.Panel(self)
        s = wx.BoxSizer(wx.VERTICAL)

        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(label(p, "Vendor download (.zip):"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, PAD)
        self.file = wx.FilePickerCtrl(p, wildcard="Zip files (*.zip)|*.zip;*.ZIP|All files|*.*",
                                      style=wx.FLP_OPEN | wx.FLP_FILE_MUST_EXIST | wx.FLP_USE_TEXTCTRL)
        top.Add(self.file, 1, wx.EXPAND)
        folder = wx.Button(p, label="Folder...")
        top.Add(folder, 0, wx.LEFT, PAD)
        s.Add(top, 0, wx.EXPAND | wx.ALL, PAD)
        num = wx.BoxSizer(wx.HORIZONTAL)
        num.Add(label(p, "or part number:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, PAD)
        self.number = wx.TextCtrl(p, style=wx.TE_PROCESS_ENTER, size=(170, -1))
        self.number.SetHint("C8734 or STM32F103C8T6")
        num.Add(self.number, 0, wx.RIGHT, PAD)
        lcsc = wx.Button(p, label="Get from LCSC")
        lcsc.SetToolTip("LCSC numbers (C...) are imported automatically: symbol, footprint and 3D model. "
                        "Any other part number opens the LCSC search so you can find its C number.")
        digikey = wx.Button(p, label="Find on DigiKey")
        digikey.SetToolTip("Opens DigiKey. On the part's page use 'EDA/CAD Models' to download the KiCad zip, "
                           "then pick it above.")
        num.Add(lcsc, 0, wx.RIGHT, PAD)
        num.Add(digikey)
        s.Add(num, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, PAD)
        self.found = label(p, "Pick a vendor zip (SnapEDA, Mouser, UltraLibrarian, DigiKey, ...) or enter an LCSC "
                              "part number.", wrap=620)
        s.Add(self.found, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, PAD)

        g = wx.FlexGridSizer(2, PAD, PAD)
        g.AddGrowableCol(1)

        def row(text, ctrl):
            g.Add(label(p, text), 0, wx.ALIGN_CENTER_VERTICAL)
            g.Add(ctrl, 1, wx.EXPAND)

        cat_row = wx.BoxSizer(wx.HORIZONTAL)
        self.cat = CategoryCombo(p, self.cats)
        self.cat.SetSelection(list(self.cats).index("Misc") if "Misc" in self.cats else 0)
        self.cat_descr = label(p, "")
        cat_row.Add(self.cat, 0, wx.RIGHT, PAD)
        cat_row.Add(self.cat_descr, 1, wx.ALIGN_CENTER_VERTICAL)
        row("Category:", cat_row)
        self.sym = wx.Choice(p)
        row("Symbol in zip:", self.sym)
        self.sym_name = wx.TextCtrl(p)
        row("Save symbol as:", self.sym_name)
        self.fp = wx.Choice(p)
        row("Footprint in zip:", self.fp)
        self.fp_name = wx.TextCtrl(p)
        row("Save footprint as:", self.fp_name)
        self.mpn = wx.TextCtrl(p)
        row("MPN:", self.mpn)
        self.mfr = wx.TextCtrl(p)
        row("Manufacturer:", self.mfr)
        self.ds = wx.TextCtrl(p)
        row("Datasheet URL:", self.ds)
        self.model = wx.Choice(p)
        row("3D model:", self.model)
        s.Add(g, 0, wx.EXPAND | wx.ALL, PAD)

        # The fields keep their natural width: on Linux (GTK) they carry -/+
        # buttons that get cut off when the field is made narrower.
        t = wx.FlexGridSizer(4, PAD, PAD)
        t.Add(label(p, "3D offset (mm):"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.off = [wx.SpinCtrlDouble(p, min=-100, max=100, inc=0.05) for _ in range(3)]
        for c in self.off:
            c.SetDigits(3)
            c.SetToolTip("Move the 3D model (mm). -/+ step by 0.05 mm.")
            t.Add(c)
        t.Add(label(p, "3D rotation (deg):"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.rot = [wx.SpinCtrlDouble(p, min=-360, max=360, inc=90) for _ in range(3)]
        for c in self.rot:
            c.SetDigits(1)
            c.SetToolTip("Turn the 3D model around X / Y / Z. -/+ turn by 90 degrees.")
            t.Add(c)
        s.Add(t, 0, wx.LEFT | wx.RIGHT | wx.TOP, PAD)
        self.reset = wx.Button(p, label="Vendor values")
        self.reset.SetToolTip("Undo your changes: back to the offset/rotation from the download")
        self.reset.Bind(wx.EVT_BUTTON, lambda e: self.vendor_values())
        s.Add(self.reset, 0, wx.ALL, PAD)
        s.Add(label(p, "Offset/rotation start as the vendor set them. Adjust them until the preview on the "
                       "right looks right (pins on the pads, part flat on the board).", wrap=620),
              0, wx.LEFT | wx.RIGHT, PAD)

        self.replace = wx.CheckBox(p, label="Replace existing part with the same name")
        s.Add(self.replace, 0, wx.ALL, PAD)
        self.problems = label(p, "", colour=wx.Colour(200, 0, 0))
        # Room for a few lines of warnings, so the window doesn't need to grow
        # (and cut off its buttons) when one appears.
        self.problems.SetMinSize((-1, self.problems.GetCharHeight() * 3 + 4))
        s.Add(self.problems, 0, wx.LEFT | wx.RIGHT, PAD)

        b = wx.BoxSizer(wx.HORIZONTAL)
        self.go = wx.Button(p, label="Import")
        b.Add(self.go, 0, wx.RIGHT, PAD)
        b.Add(wx.Button(p, wx.ID_CANCEL, label="Close"))
        s.Add(b, 0, wx.ALIGN_RIGHT | wx.ALL, PAD)

        # 3D preview, rendered by kicad-cli (wx has no STEP viewer).
        pv = wx.BoxSizer(wx.VERTICAL)
        pv.Add(label(p, "Preview", bold=True), 0, wx.BOTTOM, 4)
        grid = wx.GridSizer(3, 2, 4, 4)
        self.views = {}
        blank = self.blank_bitmap()
        for name in ["Symbol", "Footprint"] + list(importer.PREVIEW_VIEWS):
            cell = wx.BoxSizer(wx.VERTICAL)
            cell.Add(label(p, name), 0)
            bmp = wx.StaticBitmap(p, bitmap=blank)
            cell.Add(bmp, 0)
            grid.Add(cell)
            self.views[name] = bmp
        pv.Add(grid, 0)
        self.preview_status = label(p, "Pick a zip to see the part in 3D.")
        pv.Add(self.preview_status, 0, wx.TOP, 4)
        outer = wx.BoxSizer(wx.HORIZONTAL)
        outer.Add(s, 1, wx.EXPAND)
        outer.Add(pv, 0, wx.ALL, PAD)
        p.SetSizer(outer)
        outer.Fit(self)
        self.SetMinSize(self.GetSize())
        self.preview_gen = 0
        self.preview_timer = None

        self.file.Bind(wx.EVT_FILEPICKER_CHANGED, self.on_file)
        lcsc.Bind(wx.EVT_BUTTON, self.on_lcsc)
        self.number.Bind(wx.EVT_TEXT_ENTER, self.on_lcsc)
        digikey.Bind(wx.EVT_BUTTON, self.on_digikey)
        folder.Bind(wx.EVT_BUTTON, self.on_folder)
        self.sym.Bind(wx.EVT_CHOICE, lambda e: self.on_symbol())
        self.fp.Bind(wx.EVT_CHOICE, lambda e: self.on_footprint())
        for ctrl in (self.sym_name, self.fp_name):
            ctrl.Bind(wx.EVT_TEXT, lambda e: self.validate())
        self.replace.Bind(wx.EVT_CHECKBOX, lambda e: self.validate())
        self.cat.Bind(wx.EVT_COMBOBOX, lambda e: self.validate())
        self.model.Bind(wx.EVT_CHOICE, lambda e: self.schedule_preview())
        for c in self.off + self.rot:
            c.Bind(wx.EVT_SPINCTRLDOUBLE, lambda e: self.schedule_preview())
            c.Bind(wx.EVT_TEXT, lambda e: self.schedule_preview())
        self.go.Bind(wx.EVT_BUTTON, self.on_import)
        self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
        self.enable(False)
        self.validate()

    def enable(self, on):
        for c in (self.cat, self.sym, self.sym_name, self.fp, self.fp_name, self.mpn, self.mfr, self.ds,
                  self.model, self.replace, self.go, *self.off, *self.rot, self.reset):
            c.Enable(on)

    def on_destroy(self, e):
        if e.GetEventObject() is self:
            if self.dl:
                self.dl.cleanup()
            if getattr(self, "lcsc_dir", None):
                shutil.rmtree(self.lcsc_dir, ignore_errors=True)
        e.Skip()

    def on_file(self, _):
        # The text field reports every keystroke: only react to a real zip file.
        path = self.file.GetPath()
        if path.lower().endswith(".zip") and os.path.isfile(path) and path != getattr(self, "loaded", None):
            self.loaded = path
            self.load(path)

    def on_lcsc(self, _):
        number = self.number.GetValue().strip()
        if not number:
            return
        if not importer.LCSC_RE.match(number):
            webbrowser.open(importer.search_urls(number)["lcsc"])
            info(self, f"'{number}' isn't an LCSC number, so the LCSC search opened in your browser.\n\n"
                       "Copy the part's LCSC number (C...) from there and paste it here.", "Find the LCSC number")
            return
        self.lcsc_dir = getattr(self, "lcsc_dir", None) or Path(tempfile.mkdtemp(prefix="plus381-lcsc-"))
        try:
            folder = run_busy(self, f"Downloading {number.upper()} from LCSC/EasyEDA...",
                              importer.fetch_lcsc, number, self.lcsc_dir)
        except Exception as e:  # noqa: BLE001
            error(self, e, "LCSC import failed")
            return
        self.file.SetPath("")
        self.load(str(folder))

    def on_digikey(self, _):
        number = self.number.GetValue().strip()
        if not number:
            error(self, "Enter the manufacturer or DigiKey part number first.", "Part number missing")
            return
        webbrowser.open(importer.search_urls(number)["digikey"])
        info(self, "DigiKey opened in your browser. On the part's page click 'EDA/CAD Models', download the "
                   "KiCad version (free DigiKey/Ultra Librarian/SnapMagic login), then pick the zip at the "
                   "top of this window.", "Download from DigiKey")

    def on_folder(self, _):
        with wx.DirDialog(self, "Folder with the unzipped vendor files") as d:
            if d.ShowModal() == wx.ID_OK:
                self.file.SetPath("")
                self.load(d.GetPath())

    def load(self, path):
        if not path:
            return
        if self.dl:
            self.dl.cleanup()
        try:
            self.dl = run_busy(self, "Reading the download...", importer.analyze, path)
        except Exception as e:  # noqa: BLE001
            self.dl = None
            self.enable(False)
            error(self, f"Could not read {path}:\n{e}")
            return
        dl = self.dl
        self.found.SetLabel(f"Found {len(dl.symbols)} symbol(s), {len(dl.footprints)} footprint(s), "
                            f"{len(dl.models)} 3D model(s).  " + " ".join(dl.notes))
        self.found.Wrap(620)
        self.sym.Set([s.name for s in dl.symbols] + ["(no symbol)"])
        self.sym.SetSelection(0)
        self.fp.Set([f.name for f in dl.footprints] + ["(no footprint)"])
        self.fp.SetSelection(0)
        self.model.Set([m.name for m in dl.models] + ["(no 3D model)"])
        self.enable(True)
        self.on_symbol()
        self.on_footprint()

    def current(self):
        dl = self.dl
        si, fi, mi = self.sym.GetSelection(), self.fp.GetSelection(), self.model.GetSelection()
        sym = dl.symbols[si] if 0 <= si < len(dl.symbols) else None
        fp = dl.footprints[fi] if 0 <= fi < len(dl.footprints) else None
        model = dl.models[mi] if 0 <= mi < len(dl.models) and fp else None
        return sym, fp, model

    def on_symbol(self):
        sym, _, _ = self.current()
        self.sym_name.Enable(sym is not None)
        if sym:
            d = importer.symbol_defaults(sym)
            self.sym_name.SetValue(importer.safe_name(sym.name))
            self.mpn.SetValue(d["mpn"])
            self.mfr.SetValue(d["manufacturer"])
            self.ds.SetValue(d["datasheet"])
        self.validate()
        self.schedule_preview()

    def on_footprint(self):
        _, fp, _ = self.current()
        self.fp_name.Enable(fp is not None)
        self.model.Enable(fp is not None)
        if fp:
            self.fp_name.SetValue(importer.safe_name(fp.name))
            m = importer.guess_model(self.dl, fp)
            self.model.SetSelection(self.dl.models.index(m) if m else len(self.dl.models))
        self.vendor_values()
        self.validate()

    def vendor_values(self):
        """Offset/rotation as the vendor's footprint has them."""
        _, fp, _ = self.current()
        if fp:
            off, rot = importer.model_transform(fp)
            for c, v in zip(self.off + self.rot, off + rot):
                c.SetValue(v)
        self.schedule_preview()

    # ---- 3D preview

    PREVIEW_SIZE = (240, 180)

    def blank_bitmap(self):
        bmp = wx.Bitmap(*self.PREVIEW_SIZE, 24)  # no alpha: inside KiCad it would show black
        dc = wx.MemoryDC(bmp)
        dc.SetBackground(wx.Brush(wx.Colour(225, 225, 228)))
        dc.Clear()
        dc.SelectObject(wx.NullBitmap)
        return bmp

    def schedule_preview(self):
        """Re-render shortly after the last change (typing fires many events)."""
        if not self.dl:
            return
        if self.preview_timer and self.preview_timer.IsRunning():
            self.preview_timer.Stop()
        self.preview_timer = wx.CallLater(400, self.start_preview)

    def start_preview(self):
        sym, fp, model = self.current()
        if not self.dl:
            return
        self.preview_gen += 1
        gen = self.preview_gen
        drawings = (sym, fp, self.dl.tmp / "preview" / f"{gen}-2d")
        if not fp:
            self.render_drawings(gen, *drawings)
            self.preview_status.SetLabel("No footprint selected.")
            return
        args = (fp, model, [c.GetValue() for c in self.off], [c.GetValue() for c in self.rot],
                self.dl.tmp / "preview" / str(gen), self.PREVIEW_SIZE)
        self.preview_status.SetLabel("Rendering..." if gen > 1 else
                                     "Rendering (the first time takes a few seconds)...")

        def work():
            try:
                self.render_drawings(gen, *drawings)
                importer.render_preview(*args, on_view=lambda n, png: wx.CallAfter(self.show_view, gen, n, png))
                wx.CallAfter(self.preview_done, gen, None)
            except Exception as e:  # noqa: BLE001 - preview is optional
                wx.CallAfter(self.preview_done, gen, e)
        threading.Thread(target=work, daemon=True).start()

    def render_drawings(self, gen, sym, fp, outdir):
        """Symbol and footprint drawings (quick), shown before the 3D views."""
        for name, fn, item in (("Symbol", importer.render_symbol_svg, sym),
                               ("Footprint", importer.render_footprint_svg, fp)):
            svg = None
            if item:
                try:
                    svg = fn(item, outdir)
                except Exception:  # noqa: BLE001 - preview is optional
                    svg = None
            wx.CallAfter(self.show_svg, gen, name, svg)

    def show_svg(self, gen, name, svg):
        if gen != self.preview_gen or not self:
            return
        w, h = self.PREVIEW_SIZE
        bmp = None
        if svg:
            try:
                bmp = svg_on_white(Path(svg).read_bytes(), w, h)
            except Exception:  # noqa: BLE001 - preview is optional
                bmp = None
        self.views[name].SetBitmap(bmp or self.blank_bitmap())

    def show_view(self, gen, name, png):
        if gen != self.preview_gen or not self:
            return  # an older render finished after a newer one started
        img = wx.Image(str(png))
        if img.IsOk():
            self.views[name].SetBitmap(img.ConvertToBitmap())

    def preview_done(self, gen, err):
        if gen != self.preview_gen or not self:
            return
        _, _, model = self.current()
        if err:
            self.preview_status.SetLabel(f"No preview: {err}")
        elif not model:
            self.preview_status.SetLabel("No 3D model selected: showing pads only.")
        else:
            self.preview_status.SetLabel("Up to date.")

    def choices(self):
        sym, fp, model = self.current()
        return importer.Choices(
            category=list(self.cats)[self.cat.GetSelection()],
            symbol=sym, symbol_name=importer.safe_name(self.sym_name.GetValue()),
            footprint=fp, footprint_name=importer.safe_name(self.fp_name.GetValue()),
            model=model, offset=[c.GetValue() for c in self.off], rotation=[c.GetValue() for c in self.rot],
            mpn=self.mpn.GetValue().strip(), manufacturer=self.mfr.GetValue().strip(),
            datasheet=self.ds.GetValue().strip(), replace=self.replace.GetValue())

    def validate(self):
        self.cat_descr.SetLabel(list(self.cats.values())[self.cat.GetSelection()])
        if not self.dl:
            return []
        c = self.choices()
        problems = importer.check_choices(self.cfg["parts_dir"], c)
        if not c.symbol and not c.footprint:
            problems.append("Nothing selected to import.")
        self.problems.SetLabel("\n".join(problems))
        self.problems.Wrap(620)
        self.go.Enable(not problems)
        self.panel.Layout()
        return problems

    def on_import(self, _):
        if self.validate():
            return
        c = self.choices()
        try:
            written = run_busy(self, "Importing...", importer.do_import, self.cfg["parts_dir"], c)
        except Exception as e:  # noqa: BLE001
            error(self, f"Import failed:\n{e}")
            return
        nick = partsync.lib_prefix(self.cfg["parts_dir"]) + c.category
        partsync.ensure_lib_tables(self.cfg["parts_dir"])
        lines = [f"- {what}: {Path(path).name}" for what, path in written]
        if nick not in self.known_libs:
            lines.append(f"\n{nick} is a new library: close KiCad completely and open it again to see it.")
        msg = ("Imported into the library on this computer:\n" + "\n".join(lines) +
               "\n\nNext: click 'Publish' in the Plus381 toolbar to share it with the team. "
               "(To change it later, edit it in the Symbol/Footprint Editor and Publish again.)\n\n"
               "If the part doesn't appear in KiCad yet, reopen the symbol/footprint chooser.")
        info(self, msg, "Part imported")
        self.dl.cleanup()
        self.dl = None
        self.file.SetPath("")
        self.enable(False)
        blank = self.blank_bitmap()
        for bmp in self.views.values():
            bmp.SetBitmap(blank)
        self.found.SetLabel("Pick the next zip, or close this window.")


# --------------------------------------------------------------------------
# Publish
# --------------------------------------------------------------------------

class PublishDialog(wx.Dialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent, title=f"{TITLE}: Publish", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        set_icon(self, "publish")
        self.cfg = cfg
        p = wx.Panel(self)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(label(p, "Your changes to the parts library", bold=True), 0, wx.ALL, PAD)
        self.list = wx.ListCtrl(p, style=wx.LC_REPORT | wx.LC_SINGLE_SEL, size=(600, 200))
        for i, (h, w) in enumerate((("Part / file", 300), ("Type", 90), ("Change", 190))):
            self.list.InsertColumn(i, h, width=w)
        s.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, PAD)
        s.Add(label(p, "Description:"), 0, wx.LEFT | wx.TOP | wx.RIGHT, PAD)
        self.msg = wx.TextCtrl(p)
        s.Add(self.msg, 0, wx.EXPAND | wx.ALL, PAD)
        self.check = wx.TextCtrl(p, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(600, 110))
        s.Add(self.check, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, PAD)
        b = wx.BoxSizer(wx.HORIZONTAL)
        self.go = wx.Button(p, label="Publish to team")
        b.Add(self.go, 0, wx.RIGHT, PAD)
        b.Add(wx.Button(p, wx.ID_CANCEL, label="Close"))
        s.Add(b, 0, wx.ALIGN_RIGHT | wx.ALL, PAD)
        p.SetSizer(s)
        s.Fit(self)
        self.go.Bind(wx.EVT_BUTTON, self.on_publish)
        self.refresh()

    def refresh(self):
        self.list.DeleteAllItems()
        try:
            items = publish.pending_changes(self.cfg)
        except GitError as e:
            items = []
            self.check.SetValue(str(e))
        for name, kind, change, _ in items:
            i = self.list.InsertItem(self.list.GetItemCount(), name)
            self.list.SetItem(i, 1, kind)
            self.list.SetItem(i, 2, change)
        self.items = items
        self.msg.SetValue(publish.default_message(items) if items else "")
        self.go.Enable(bool(items))
        if not items:
            self.check.SetValue("Nothing to publish: your library matches the team's.")
            return
        mine, other = self.problems()
        text = "Library check: no problems in your changes." if not mine else \
            "Problems in YOUR changes (fix before publishing):\n" + "\n".join(mine)
        if other:
            text += f"\n\n({len(other)} older problem(s) elsewhere in the library, not caused by you - " \
                    "see Library status > Check library.)"
        self.check.SetValue(text)

    def problems(self):
        """Library check errors, split into ones about the parts being published and the rest."""
        _, out = publish.run_check(self.cfg)
        errors = [l[len("ERROR: "):] for l in out.splitlines() if l.startswith("ERROR")]
        names = {i[0] for i in self.items} | {Path(i[3]).name for i in self.items}
        mine = [e for e in errors if any(n in e for n in names)]
        return mine, [e for e in errors if e not in mine]

    def on_publish(self, _):
        mine, _ = self.problems()
        if mine:
            if wx.MessageBox("Your changes have problems (see the list). Teammates may get broken parts.\n\n"
                             "Publish anyway?", TITLE, wx.YES_NO | wx.ICON_WARNING, self) != wx.YES:
                return
        login = self.ensure_login()
        if login is False:
            return
        self.token, self.mode = login or (None, "direct")
        if publish.identity_missing(self.cfg):
            name = wx.GetTextFromUser("Your name (shown next to your changes):", TITLE, parent=self)
            email = wx.GetTextFromUser("The e-mail of your GitHub account:", TITLE, parent=self)
            if not (name and email):
                return
            publish.set_identity(self.cfg, name, email)
        message = self.msg.GetValue().strip() or "Update parts"
        try:
            url = run_busy(self, "Publishing...", publish.publish, self.cfg, message, self.token, self.mode)
        except Conflict as c:
            self.conflict(c)
        except Exception as e:  # noqa: BLE001
            hint = ""
            if re.search(r"auth|credential|permission|403|could not read username", str(e), re.I):
                hint = ("\n\nGitHub refused the upload. Open Library status > Log in to GitHub and try again.")
            error(self, f"Publishing failed:\n{e}{hint}\n\nYour changes are saved locally; try again later.")
        else:
            self.done(url)
        self.refresh()

    def ensure_login(self):
        """(token, mode) when logged in, None when in-plugin login isn't
        configured (fall back to whatever login Git has), False if cancelled."""
        if not github.client_id():
            return None
        try:
            token = run_busy(self, "Checking your GitHub login...", github.get_token, self.cfg)
            if token:
                mode = run_busy(self, "Checking your GitHub login...",
                                lambda: "direct" if github.can_push(token) else "pr")
                return token, mode
        except github.GitHubError as e:
            if " 401 " not in str(e) + " ":
                error(self, e, "Can't reach GitHub")
                return False
            # 401: the saved login was revoked or expired, so log in again
        if wx.MessageBox("To publish, log in to GitHub once. It takes a few seconds in your browser "
                         "and KiCad remembers it.\n\nLog in now?", TITLE, wx.YES_NO | wx.ICON_QUESTION,
                         self) != wx.YES:
            return False
        result = login_dialog(self, self.cfg)
        if not result:
            return False
        me, mode = result
        token = github.get_token(self.cfg)
        return token, mode

    def done(self, url):
        if url:
            if wx.MessageBox("Your account can't change the team library directly, so your parts were sent as "
                             "a pull request. A team member checks and merges it, then everyone gets them.\n\n"
                             f"{url}\n\nOpen it in the browser?", f"{TITLE}: Sent for review",
                             wx.YES_NO | wx.ICON_INFORMATION, self) == wx.YES:
                webbrowser.open(url)
        else:
            info(self, "Published. Everyone gets your changes within a minute.", "Published")

    def conflict(self, c):
        names = "\n".join("- " + publish.describe(f)[0] for f in c.files)
        dlg = wx.MessageDialog(self, "Someone else changed the same part(s) while you were working:\n" + names +
                               "\n\nWhich version should the team keep?", TITLE,
                               wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION)
        dlg.SetYesNoCancelLabels("Keep mine", "Keep theirs", "Decide later")
        ans = dlg.ShowModal()
        dlg.Destroy()
        try:
            if ans == wx.ID_YES:
                mode = getattr(self, "mode", "direct")
                run_busy(self, "Publishing your version...", publish.resolve, self.cfg, "mine", mode != "pr")
                url = None
                if mode == "pr":
                    url, _ = run_busy(self, "Sending for review...", github.open_pull_request, self.cfg,
                                      self.token, self.msg.GetValue().strip() or "Update parts",
                                      publish._pr_body(self.cfg))
                self.done(url)
            elif ans == wx.ID_NO:
                backup = run_busy(self, "Taking the team's version...", publish.resolve, self.cfg, "theirs")
                info(self, f"Took the team's version. Your version was saved in:\n{backup}")
        except Exception as e:  # noqa: BLE001
            error(self, e)


# --------------------------------------------------------------------------
# GitHub login
# --------------------------------------------------------------------------

class LoginDialog(wx.Dialog):
    """Shows the one-time code for GitHub's device login and waits for the
    user to approve it in the browser."""

    def __init__(self, parent, start):
        super().__init__(parent, title=f"{TITLE}: Log in to GitHub")
        set_icon(self, "publish")
        self.start = start
        self.token = None
        self.err = None
        self.cancelled = False
        p = wx.Panel(self)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(label(p, "1. Click 'Open GitHub' (or go to " + github.device_page(start) + ")\n"
                       "2. Enter this code and click Continue / Authorize:"), 0, wx.ALL, PAD)
        code = wx.StaticText(p, label=start["user_code"])
        f = code.GetFont()
        f.SetPointSize(f.GetPointSize() * 2)
        code.SetFont(f.Bold())
        s.Add(code, 0, wx.ALIGN_CENTER | wx.ALL, PAD * 2)
        self.status = label(p, "Waiting for you to approve in the browser...")
        s.Add(self.status, 0, wx.ALL, PAD)
        b = wx.BoxSizer(wx.HORIZONTAL)
        copy = wx.Button(p, label="Copy code")
        open_ = wx.Button(p, label="Open GitHub")
        cancel = wx.Button(p, wx.ID_CANCEL, label="Cancel")
        for btn in (copy, open_, cancel):
            b.Add(btn, 0, wx.LEFT, PAD)
        s.Add(b, 0, wx.ALIGN_RIGHT | wx.ALL, PAD)
        p.SetSizer(s)
        s.Fit(self)
        copy.Bind(wx.EVT_BUTTON, self.on_copy)
        open_.Bind(wx.EVT_BUTTON, lambda e: webbrowser.open(github.device_page(start)))
        self.Bind(wx.EVT_BUTTON, self.on_cancel, id=wx.ID_CANCEL)
        open_.SetDefault()
        threading.Thread(target=self.wait, daemon=True).start()

    def on_copy(self, _):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(self.start["user_code"]))
            wx.TheClipboard.Close()
            self.status.SetLabel("Code copied. Paste it on the GitHub page.")

    def on_cancel(self, _):
        self.cancelled = True
        self.EndModal(wx.ID_CANCEL)

    def wait(self):
        try:
            self.token = github.wait_for_login(self.start, lambda: self.cancelled)
        except github.GitHubError as e:
            self.err = e
        if not self.cancelled:
            wx.CallAfter(self.EndModal, wx.ID_OK if self.token else wx.ID_ABORT)


def login_dialog(parent, cfg):
    """Run the whole login. Returns (user, mode) or None."""
    try:
        start = run_busy(parent, "Contacting GitHub...", github.start_login)
    except github.GitHubError as e:
        error(parent, e, "Login not possible")
        return None
    webbrowser.open(github.device_page(start))
    dlg = LoginDialog(parent, start)
    dlg.ShowModal()
    token, err = dlg.token, dlg.err
    dlg.Destroy()
    if err:
        error(parent, err, "Login failed")
    if not token:
        return None
    try:
        me, mode = run_busy(parent, "Finishing login...", github.after_login, cfg, token)
    except Exception as e:  # noqa: BLE001
        error(parent, e, "Login failed")
        return None
    how = ("You can publish straight to the team library." if mode == "direct" else
           "Your account isn't a member of plus381-robotics, so your parts will be sent as pull requests "
           "for the team to review.")
    info(parent, f"Logged in to GitHub as {me['login']}.\n\n{how}", "Logged in")
    return me, mode


# --------------------------------------------------------------------------
# Library status
# --------------------------------------------------------------------------

class StatusDialog(wx.Dialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent, title=f"{TITLE}: Library status", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        set_icon(self, "status")
        self.cfg = cfg
        p = self.panel = wx.Panel(self)
        s = wx.BoxSizer(wx.VERTICAL)
        self.text = wx.TextCtrl(p, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(620, 300))
        s.Add(self.text, 1, wx.EXPAND | wx.ALL, PAD)
        b = wx.WrapSizer()
        buttons = [("Sync now", self.on_sync), ("Check library", self.on_check),
                   ("Open library folder", lambda e: publish.open_folder(cfg["parts_dir"])),
                   ("Background sync on/off", self.on_toggle), ("Run setup again", self.on_setup),
                   ("Log in to GitHub", self.on_login), ("Log out", self.on_logout)]
        for text, fn in buttons:
            btn = wx.Button(p, label=text)
            btn.Bind(wx.EVT_BUTTON, fn)
            b.Add(btn, 0, wx.ALL, 3)
        b.Add(wx.Button(p, wx.ID_CANCEL, label="Close"), 0, wx.ALL, 3)
        s.Add(b, 0, wx.ALL, PAD - 3)
        p.SetSizer(s)
        s.Fit(self)
        self.refresh()

    def refresh(self, extra=""):
        cfg = self.cfg
        st = partsync.load_status()
        libs = partsync.libraries(cfg["parts_dir"])
        n_sym = sum(len(list(v["sym"].glob("*.kicad_sym"))) for v in libs.values() if "sym" in v)
        n_fp = sum(len(list(v["fp"].glob("*.kicad_mod"))) for v in libs.values() if "fp" in v)
        n_3d = len([m for m in (Path(cfg["parts_dir"]) / "3dmodels").glob("*") if m.name != ".gitkeep"])
        try:
            pending = len(publish.pending_changes(cfg))
        except GitError:
            pending = "?"
        auto = autosync.installed()
        lines = [
            f"Repository:       {partsync.repo_url(cfg)}",
            f"Library folder:   {cfg['parts_dir']}",
            f"Contents:         {len(libs)} libraries, {n_sym} symbols, {n_fp} footprints, {n_3d} 3D models",
            f"Last sync:        {st.get('time', 'never')}  {'OK' if st.get('ok') else 'PROBLEM'}",
            f"Last new parts:   {st.get('last_update') or '-'}",
            f"Background sync:  {'on (every minute while KiCad is open)' if auto else 'OFF - library only updates when you use the plugin'}",
            f"Path variable:    {'ok' if partsync.env_var_ok(cfg['parts_dir']) else 'not set yet - close KiCad completely and open it again'}",
            f"Unpublished:      {pending} change(s)",
            f"GitHub:           {self.github_line()}",
        ]
        if st.get("message"):
            lines += ["", "Message: " + st["message"]]
        for p in st.get("kicad_problems") or []:
            lines.append("! " + p)
        if st.get("last_new_files"):
            lines += ["", "Recently received:"] + ["  " + publish.describe(f)[0] for f in st["last_new_files"][:15]]
        if extra:
            lines += ["", extra]
        self.text.SetValue("\n".join(lines))

    def github_line(self):
        c = partsync.load_config()
        if not c.get("github_login"):
            return "not logged in (only needed to publish)"
        how = "publishes via pull request" if c.get("publish_mode") == "pr" else "publishes directly"
        return f"{c['github_login']} ({how})"

    def on_login(self, _):
        login_dialog(self, self.cfg)
        self.refresh()

    def on_logout(self, _):
        try:
            github.logout(self.cfg)
        except Exception as e:  # noqa: BLE001
            error(self, e)
        self.refresh("Logged out of GitHub.")

    def on_sync(self, _):
        try:
            st = run_busy(self, "Syncing...", partsync.run_sync, self.cfg, True)
        except Exception as e:  # noqa: BLE001
            error(self, e)
            st = {}
        self.refresh(f"Received {len(st.get('new_files', []))} changed file(s)." if st.get("ok") else "")

    def on_check(self, _):
        ok, out = publish.run_check(self.cfg)
        self.refresh("Library check:\n" + out)

    def on_toggle(self, _):
        try:
            if autosync.installed():
                autosync.uninstall()
            else:
                autosync.install()
        except Exception as e:  # noqa: BLE001
            error(self, e)
        self.refresh()

    def on_setup(self, _):
        dlg = SetupDialog(self)
        dlg.ShowModal()
        dlg.Destroy()
        self.cfg = partsync.load_config()
        self.refresh()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

_app = None


def main(action, parent=None):
    """Run one action. Called by the API entry scripts (own process) and by
    the fallback buttons in the PCB editor (inside KiCad, parent = editor)."""
    global _app, CURRENT_ICON
    CURRENT_ICON = {"import": "import", "publish": "publish", "setup": "setup"}.get(action, "status")
    if not wx.GetApp():
        _app = wx.App(False)  # must stay alive while the dialogs run
        # Linux taskbars name and group windows by this (else "import_part.py").
        _app.SetAppName("plus381-tools")
        _app.SetClassName("Plus381 Tools")
        _app.SetAppDisplayName(TITLE)
    try:
        cfg = ensure_setup(parent)
        if not cfg:
            return
        autosync.refresh_script()
        if sys.prefix != sys.base_prefix:
            # Remember the plugin's environment for the fallback buttons
            # (it has easyeda2kicad installed).
            c = partsync.load_config()
            if c.get("plugin_python") != sys.executable:
                c["plugin_python"] = sys.executable
                partsync.save_config(c)
        if action != "setup":
            st = quick_sync(parent, cfg)
            if not st.get("ok") and st.get("message"):
                wx.MessageBox("Could not get the newest parts (you can keep working):\n\n" + st["message"],
                              TITLE, wx.OK | wx.ICON_WARNING, parent)
        dlg = {"import": ImportDialog, "publish": PublishDialog, "status": StatusDialog}.get(action, StatusDialog)
        d = dlg(cfg, parent)
        d.ShowModal()
        d.Destroy()
    except Exception:  # noqa: BLE001 - last resort: show instead of dying silently
        wx.MessageBox(traceback.format_exc(), f"{TITLE}: unexpected error", wx.OK | wx.ICON_ERROR)
