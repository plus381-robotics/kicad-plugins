"""Start an action so that a failure is always seen.

The API buttons run in their own process: if that dies on startup (a missing
module, say), KiCad shows nothing at all. So anything that goes wrong before
the windows open is shown in a message box and appended to plugin.log in the
Plus381 settings folder (Windows: %APPDATA%\\plus381\\plugin.log).
Standard library only, so this works even when the rest can't be imported.
"""

import datetime
import os
import sys
import traceback


def _log_path():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "plus381", "plugin.log")


def _report(action):
    tb = traceback.format_exc()
    log = _log_path()
    try:
        from . import __version__
    except Exception:  # noqa: BLE001
        __version__ = "?"
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"--- {datetime.datetime.now():%Y-%m-%d %H:%M:%S} starting '{action}' "
                    f"(Plus381 Tools {__version__}, Python {sys.version.split()[0]}, {sys.executable}, "
                    f"{sys.platform})\n{tb}\n")
    except OSError:
        pass
    text = f"{tb}\nAlso saved to {log}"
    sys.stderr.write(text + "\n")
    try:
        import wx
        if not wx.GetApp():
            wx.App(False)
        wx.MessageBox(text, "Plus381 Tools: could not start", wx.OK | wx.ICON_ERROR)
        return
    except Exception:  # noqa: BLE001 - wx itself may be what's broken
        pass
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Plus381 Tools: could not start", 0x10)


def run(action, parent=None):
    try:
        from . import ui
    except Exception:  # noqa: BLE001
        _report(action)
        return
    ui.main(action, parent=parent)
