# Plus381 KiCad plugins

KiCad 10 plugins for the Plus381 Robotics team.

| Plugin | What it does |
|---|---|
| [`plus381-tools`](plus381-tools/) | Connects KiCad to the team parts library ([kicad-parts](https://github.com/plus381-robotics/kicad-parts)): import vendor parts, publish them, keep everyone's library up to date automatically |

## Install (once per computer, about 5 minutes)

You need **KiCad 10**, **Git with Git LFS**, and a GitHub account in the
plus381-robotics organisation.

* **Windows:** [Git for Windows](https://git-scm.com/download/win) (LFS is
  included) or GitHub Desktop.
* **macOS:** `brew install git git-lfs`
* **Linux:** install `git` and `git-lfs` from your package manager.

1. **Log in to GitHub once** so Git can download the private parts library.
   On Windows, Git opens a browser login the first time. Otherwise, sign in
   with GitHub Desktop, or run `git clone https://github.com/plus381-robotics/kicad-parts`
   once in a terminal and log in, then delete that folder.
2. KiCad → **Preferences → Preferences → Plugins** → tick **Enable KiCad API**,
   then click OK.
3. KiCad → **Plugin and Content Manager → Manage** (top right) → **+** and add
   `https://plus381-robotics.github.io/kicad-plugins/repository.json`.
   Choose that repository in the drop-down, find **Plus381 Tools**, click
   **Install**, then **Apply Pending Changes**.
4. Open the PCB Editor. Three new toolbar buttons appear on the right:
   **Import part**, **Library status** and **Publish**. Click any of them:
   the **Setup** window opens. Check the folder, your name and your GitHub
   e-mail, and click **Set up**.
5. **Close KiCad completely and open it again.** All `Plus381_*` libraries now
   appear in the symbol and footprint choosers of every project.

The first click on a button after installing or updating takes a few seconds
longer while KiCad prepares the plugin.

## Daily use

| Task | How |
|---|---|
| Get new parts | Nothing to do. The library updates itself every minute, even when KiCad is closed. Reopen the footprint/symbol chooser to see new parts. |
| Add a part | Download the part's KiCad zip from SnapEDA / Mouser / UltraLibrarian / DigiKey → **Import part** → pick the zip and a category → **Import** → open the footprint in the Footprint Editor, press `Alt+3`, fix the 3D model if needed and save → **Publish**. |
| Fix or improve a part | Edit it in the Symbol / Footprint Editor (it's already in the `Plus381_*` libraries), save, then **Publish**. |
| See what's going on | **Library status**: last sync, background sync on/off, unpublished changes, library check, re-run setup. |

Publishing goes straight to the team library. If someone else changed the same
part at the same time, the plugin asks whose version to keep and backs up
the other one.

## How it works

* **Setup** clones `kicad-parts` (by default to `<home>/KiCad/plus381-parts`)
  and adds every `Plus381_*` library to KiCad's global library tables. It also
  sets the KiCad path variable `PLUS381_PARTS`, which footprints use for their
  3D models (`${PLUS381_PARTS}/3dmodels/...`). KiCad rewrites its settings when
  it closes, so the variable is written by a small helper right after KiCad
  exits. That's why you restart KiCad once.
* **Background sync** is a job in your operating system's scheduler (Windows
  Task Scheduler, a macOS LaunchAgent, or a systemd user timer / cron on
  Linux). It runs `partsync.py` every minute with KiCad's own Python. It only
  fast-forwards: it never overwrites files you are editing and never
  publishes on its own, except for retrying a Publish that failed while
  you were offline. It also re-registers new categories and repairs the path
  variable if needed.
* **Import** converts the vendor files to the current KiCad format with
  `kicad-cli`. It stores the symbol and footprint as one file each (so two
  people adding parts never conflict) and links symbol → footprint → 3D
  model. It reuses an identical 3D model instead of storing a second copy, and
  keeps the vendor's 3D offset and rotation.
* Projects reference parts as `Plus381_<Category>:<Name>`. KiCad also keeps a
  copy of every part inside the schematic/board, so editing a part never
  changes existing boards until someone runs *Update from Library*.

Settings and logs live in `%APPDATA%\plus381` (Windows),
`~/Library/Application Support/plus381` (macOS) or `~/.config/plus381` (Linux).

## Uninstall

**Library status → Background sync on/off** (turn it off), uninstall the
plugin in the Plugin and Content Manager, and remove the `Plus381_*` entries
in **Preferences → Manage Symbol/Footprint Libraries**.

## Development

```text
plus381-tools/
  plugin.json            KiCad API plugin manifest (3 toolbar actions)
  pcm.json               Plugin and Content Manager metadata
  import_part.py, publish.py, library_status.py   entry points
  plus381/partsync.py    git sync + KiCad settings (standard library only; also run by the scheduler)
  plus381/autosync.py    installs the background job per OS
  plus381/importer.py    vendor zip -> library part
  plus381/publish.py     commit / push / conflict handling
  plus381/ui.py          wxPython windows
pcm/build.py             builds the PCM repository into dist/
```

* Try changes without packaging: copy `plus381-tools/` to
  `Documents/KiCad/10.0/plugins/` (Linux: `~/.local/share/kicad/10.0/plugins/`)
  and restart the PCB Editor.
* `PLUS381_PARTS_REPO=<path or URL>` makes Setup clone another library
  repository (for testing against a copy).
* **Release:** bump `__version__` in `plus381-tools/plus381/__init__.py` and
  push to `main`. The *Publish KiCad PCM repository* workflow rebuilds the
  GitHub Pages site, and KiCad offers the update in the Plugin and Content
  Manager.
* One-time repo setting: **Settings → Pages → Source: GitHub Actions**.
