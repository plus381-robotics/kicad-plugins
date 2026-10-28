"""Red dot on the Plus381 toolbar buttons while the team has new parts.

Runs inside KiCad (loaded with the fallback buttons when the PCB editor
opens), so it can change KiCad's own toolbars: every few seconds it reads
the status file that the background check writes (partsync.run_check) and
puts a red dot on the Library status button (and the fallback 381 button),
or takes it off again after an update. It works in every KiCad window of
that process, so also in the schematic editor when it was opened from the
project manager.
"""

import wx
import wx.aui

from . import partsync

EVERY_MS = 5000
MARK = ("Plus381: Library status", "Plus381 Tools (fallback)")  # buttons (by tooltip) that get the dot
SEP = "\n\nThe team has "   # tooltip = original tooltip + SEP + what's new, while dotted
_originals = {}             # original tooltip -> bitmap before the dot
_timer = None


def _with_dot(bmp):
    img = bmp.ConvertToImage()
    if not img.HasAlpha():
        img.InitAlpha()
    out = wx.Bitmap(img)
    w = out.GetWidth()
    dc = wx.MemoryDC(out)
    gc = wx.GraphicsContext.Create(dc)
    d = max(7, w * 3 // 8)
    gc.SetBrush(wx.Brush(wx.Colour(220, 30, 30)))
    gc.SetPen(wx.Pen(wx.WHITE, 1))
    gc.DrawEllipse(w - d, 0, d - 1, d - 1)
    dc.SelectObject(wx.NullBitmap)
    return out


def _toolbars():
    for top in wx.GetTopLevelWindows():
        stack = [top]
        while stack:
            w = stack.pop()
            stack.extend(w.GetChildren())
            if isinstance(w, wx.aui.AuiToolBar):
                yield w


def _news(n, names):
    parts = ", ".join(names[:5]) + (", ..." if len(names) > 5 else "")
    what = f"new or changed parts: {parts}" if parts else f"{n} changed library file(s)"
    return f"{what}.\nOpen Library status and click Update library."


def show(n, names=()):
    """Put the dot on (n > 0) or take it off (n == 0)."""
    for bar in _toolbars():
        changed = False
        for i in range(bar.GetToolCount()):
            tool = bar.FindToolByIndex(i)
            help_ = tool.GetShortHelp() or ""
            base = help_.split(SEP)[0]
            if not base.startswith(MARK):
                continue
            dotted = SEP in help_
            if n:
                if not dotted:
                    bmp = tool.GetBitmap()
                    if not bmp.IsOk():
                        continue
                    _originals[base] = bmp
                    bar.SetToolBitmap(tool.GetId(), wx.BitmapBundle(_with_dot(bmp)))
                    changed = True
                tip = base + SEP + _news(n, list(names))
                if help_ != tip:
                    tool.SetShortHelp(tip)
            elif dotted:
                if base in _originals:
                    bar.SetToolBitmap(tool.GetId(), wx.BitmapBundle(_originals[base]))
                    changed = True
                tool.SetShortHelp(base)
        if changed:
            bar.Refresh()


def _tick(_=None):
    try:
        st = partsync.load_status()
        show(int(st.get("updates") or 0), st.get("update_parts") or [])
    except Exception:  # noqa: BLE001 - never disturb KiCad
        pass


def start():
    global _timer
    if _timer is None:
        _timer = wx.Timer()
        _timer.Bind(wx.EVT_TIMER, _tick)
        _timer.Start(EVERY_MS)
