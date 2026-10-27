"""Fallback buttons for the PCB editor (classic pcbnew action plugins).

KiCad sometimes doesn't show the toolbar buttons of API plugins (or the
KiCad API is switched off). KiCad also loads this file from the same
plugin folder as a classic action plugin, which gives:

  - one toolbar button, "Plus381 Tools (fallback)", that lets you pick
    Import part / Publish / Library status, and
  - the three actions under Tools > External Plugins.

They open the same windows, running inside the PCB editor itself.
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
    import wx
    try:
        from plus381 import ui
    except Exception:  # noqa: BLE001 - show why instead of failing silently
        import traceback
        wx.MessageBox(traceback.format_exc(), "Plus381 Tools: could not start", wx.OK | wx.ICON_ERROR)
        return
    ui.main(action, parent=_editor_window())


if pcbnew is not None:

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
            dlg = wx.SingleChoiceDialog(_editor_window(), "What do you want to do?", "Plus381 Tools",
                                        [label for _, label in ACTIONS])
            dlg.SetSize((320, 300))
            if dlg.ShowModal() == wx.ID_OK:
                action = ACTIONS[dlg.GetSelection()][0]
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
