"""Fallback buttons for the PCB editor (classic pcbnew action plugins).

KiCad sometimes doesn't show the toolbar buttons of API plugins (or the
KiCad API is switched off). KiCad also loads this file from the same
plugin folder as a classic action plugin, which gives:

  - one toolbar button, "Plus381 Tools (fallback)", that lets you pick
    Import part / Publish / Library status, and
  - the three actions under Tools > External Plugins.

They open the same windows, running inside the PCB editor itself.

Loading this file also starts the background check for new parts (every 2
minutes while KiCad is open) and the red dot that the Plus381 buttons get
when the team has new parts (see plus381/badge.py).
"""

import os
import sys

try:
    import pcbnew
except ImportError:  # not inside KiCad: nothing to register
    pcbnew = None

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

ACTIONS = (("import", "Import part"), ("publish", "Publish changes"), ("status", "Library status"))


def _editor_window():
    import wx
    for w in wx.GetTopLevelWindows():
        if w.IsShown() and "PCB Editor" in w.GetTitle():
            return w
    return None


def _run(action):
    from plus381 import start
    start.run(action, parent=_editor_window())


if pcbnew is not None:
    try:
        from plus381 import partsync
        if partsync.is_repo(partsync.load_config()):
            partsync.start_watcher()
            from plus381 import badge
            badge.start()
    except Exception:  # noqa: BLE001 - never break loading the PCB editor
        pass

    class _Chooser(pcbnew.ActionPlugin):
        def defaults(self):
            self.name = "Plus381 Tools (fallback)"
            self.category = "Plus381 Tools"
            self.description = "Import part, publish changes or open the library status"
            self.show_toolbar_button = True
            self.icon_file_name = os.path.join(_HERE, "icons", "fallback-light-24.png")
            self.dark_icon_file_name = os.path.join(_HERE, "icons", "fallback-dark-24.png")

        def Run(self):
            import wx
            from plus381 import partsync
            actions = list(ACTIONS)
            n = int(partsync.load_status().get("updates") or 0)
            if n:  # the button shows a red dot: offer the update first
                actions.insert(0, ("update", "Update library (the team has new parts)"))
            dlg = wx.SingleChoiceDialog(_editor_window(), "What do you want to do?", "Plus381 Tools",
                                        [label for _, label in actions])
            dlg.SetSize((360, 300))
            if dlg.ShowModal() == wx.ID_OK:
                action = actions[dlg.GetSelection()][0]
                dlg.Destroy()
                _run(action)
            else:
                dlg.Destroy()

    def _menu_action(action, label):
        class _Action(pcbnew.ActionPlugin):
            def defaults(self):
                self.name = f"Plus381: {label} (fallback)"
                self.category = "Plus381 Tools"
                self.description = label
                self.show_toolbar_button = False

            def Run(self):
                _run(action)
        return _Action

    _Chooser().register()
    for _action, _label in ACTIONS:
        _menu_action(_action, _label)().register()
