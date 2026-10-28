# Plus381 Tools

KiCad 10 plugin that connects KiCad to a shared parts library on GitHub
(by default the team library [kicad-parts](https://github.com/plus381-robotics/kicad-parts)):
import parts, publish them, and keep everyone's library up to date
automatically. Install it as described in the [main README](../README.md).

## Install (once per computer, about 5 minutes)

You need **KiCad 10** and **Git with Git LFS**. Using parts needs no account,
since `kicad-parts` is public. To **publish** you need any GitHub account and
log in once from inside KiCad (see *Logging in* below).

* **Windows:** [Git for Windows](https://git-scm.com/download/win) (LFS is
  included) or GitHub Desktop.
* **macOS:** `brew install git git-lfs`
* **Linux:** install `git` and `git-lfs` from your package manager.

1. KiCad → **Preferences → Preferences → Plugins** → tick **Enable KiCad API**,
   then click OK.
2. KiCad → **Plugin and Content Manager → Manage** (top right) → **+** and add
   `https://plus381-robotics.github.io/kicad-plugins/repository.json`.
   Choose that repository in the drop-down, find **Plus381 Tools**, click
   **Install**, then **Apply Pending Changes**.
3. Open the PCB Editor. Three new toolbar buttons appear on the right:
   **Import part**, **Library status** and **Publish**. Click any of them:
   the **Setup** window opens. Check the folder, your name and your GitHub
   e-mail, and click **Set up**.
4. **Close KiCad completely and open it again.** All `Plus381_*` libraries now
   appear in the symbol and footprint choosers of every project.

The first click on a button after installing or updating takes a few seconds
longer while KiCad prepares the plugin.

### If the buttons don't show

KiCad sometimes shows the three Plus381 buttons late, or not at all (they
also need **Preferences → Plugins → Enable KiCad API**). The plugin
therefore adds a **fallback** that KiCad loads directly into the PCB editor:

* a **381** toolbar button that asks what to do (Import part / Publish
  changes / Library status), and
* **Tools → External Plugins → Plus381: … (fallback)** menu entries.

They open the same windows. Only importing by LCSC number needs the normal
buttons to have run once first (KiCad installs the LCSC converter for them).

### Troubleshooting

* **Nothing happens when you click a button:** errors are shown in a message
  box and saved to `plugin.log` in the settings folder (Windows:
  `%APPDATA%\plus381\plugin.log`). Send that file when asking for help.
* **"Execution of command `...\KiCad\9.0\bin\pythonw.exe -m venv ...` failed":**
  KiCad still points at the Python of an old KiCad. In **Preferences →
  Plugins**, click *Detect automatically* next to *Python interpreter* (or
  pick `C:\Program Files\KiCad\10.0\bin\pythonw.exe`), close KiCad, delete
  `%LOCALAPPDATA%\KiCad\10.0\python-environments\com.plus381.tools` and
  start KiCad again.
* **Publish says "Nothing to publish" after an edit:** see *Fix or improve a
  part* below; the Publish window also lists the likely reason.

## Daily use

| Task | How |
|---|---|
| Get new parts | When the team has new or changed parts, the **Library status** button (and the fallback **381** button) gets a **red dot**; its tooltip names the parts. Click **Library status → Update library** (or **381 → Update library**). Nothing changes in your library until you do. Then reopen the footprint/symbol chooser. |
| Add a part from LCSC | **Import part** → type the LCSC number (e.g. `C8734`) → **Get from LCSC** (or Enter). Symbol, footprint and 3D model are downloaded and converted automatically (via [easyeda2kicad](https://github.com/uPesy/easyeda2kicad.py)); the `LCSC Part` field is kept for JLCPCB assembly. Typing a manufacturer part number instead opens the LCSC search so you can find the C number. |
| Add a part from a vendor zip | Download the part's KiCad zip from SnapEDA / Mouser / UltraLibrarian / DigiKey → **Import part** → pick the zip. **Find on DigiKey** opens DigiKey for a part number; there, *EDA/CAD Models* downloads the KiCad zip (needs a free login; DigiKey has no API for CAD files). |
| Then, for both | Pick a category (each shows its description) → check the **preview** (symbol, footprint and 3D from four sides; fix the 3D offset/rotation, **Vendor values** undoes your changes) → **Import** → **Publish**. |
| Fix or improve a part | Open it from its `Plus381_*` library in the Symbol / Footprint Editor, edit, save, then **Publish**. Editing a footprint from a board (*Edit Footprint* / *Open in Footprint Editor*) only changes that board: use *Save As* into the `Plus381_*` library to share it. |
| See what's going on | **Library status**: new parts waiting, update, unpublished changes, library check, re-run setup, checking on/off. |

If someone else changed the same part at the same time, the plugin asks
whose version to keep and backs up the other one.

## Logging in (only for publishing)

The first time you click **Publish**, the plugin asks you to log in:

1. A window shows a code like `WDJB-MJHT` and your browser opens
   github.com/login/device.
2. Enter the code and click **Authorize**. KiCad continues by itself.

You log in once per computer. GitHub doesn't expire this login; it lasts
until you click **Log out** (or revoke it on GitHub). The plugin keeps it
itself, in its settings folder: encrypted for your Windows user on Windows,
in a file only you can read on macOS/Linux. It is handed to Git only for the
plugin's own commands, so other tools (GitHub Desktop, VS Code, Git
Credential Manager) can't replace or remove it. **Library status** shows who
is logged in and has **Log in to GitHub** / **Log out** buttons.

* **Members of plus381-robotics** publish straight into the team library.
* **Anyone else** (alumni, other teams) can publish too: the plugin creates
  their own copy (fork) of `kicad-parts` and opens a **pull request**. A member
  reviews and merges it, then everyone gets the part. Publishing again
  before it's merged updates the same pull request. The parts stay usable on
  the author's computer while they wait for review.

## Using your own library

Setup has a **Library repository** field. Leave the default for the Plus381
team library, or enter the address of your own copy of
[kicad-parts](https://github.com/plus381-robotics/kicad-parts) (fork it or use
it as a template). The library names come from that repository's
`categories.json` (`prefix`), and publishing, pull requests and the GitHub
login all follow the chosen repository. To switch an existing computer, run
Setup again (**Library status → Run setup again**) with the new address and
a new, empty library folder.

## How it works

* **Setup** clones `kicad-parts` (by default to `<home>/KiCad/plus381-parts`)
  and adds every `Plus381_*` library to KiCad's global library tables. It also
  sets the KiCad path variable `PLUS381_PARTS`, which footprints use for their
  3D models (`${PLUS381_PARTS}/3dmodels/...`). KiCad rewrites its settings when
  it closes, so the variable is written by a small helper right after KiCad
  exits. That's why you restart KiCad once.
* **Checking for new parts** is a small background process (`partsync.py
  watch`, KiCad's own Python). Every 2 minutes while KiCad is open it asks
  GitHub whether the team has new parts (`git fetch` only: your library
  doesn't change) and writes the answer to `status.json`. Inside KiCad, the
  plugin reads that file and draws the red dot on its toolbar buttons.
  KiCad can't start plugins by itself, so the check starts when the PCB
  editor opens (KiCad loads the plugin's fallback buttons there) or when you
  click any Plus381 button; a second copy sees the first and quits. When
  KiCad closes, it writes KiCad's settings (the path variable) and exits.
  The red dot needs the PCB editor to have been opened once in that KiCad
  session (the schematic editor can't run plugin code); **Library status**
  always checks when it opens. No system scheduler is used (versions before
  0.7 used one; it is removed automatically).
* **Update library** fast-forwards to the team's version. It never
  overwrites files you are editing. Publishing also brings in the team's
  newest version (your changes go on top of it).
* **Import** converts the vendor files to the current KiCad format with
  `kicad-cli`. It stores the symbol and footprint as one file each (so two
  people adding parts never conflict) and links symbol → footprint → 3D
  model. It reuses an identical 3D model instead of storing a second copy, and
  keeps the vendor's 3D offset and rotation. The 3D preview (3D, front, right
  and top views) is rendered by `kicad-cli pcb render` on a tiny temporary
  board holding just the part, so it shows exactly what KiCad will show. The
  first render reads the STEP file and takes a few seconds; after that,
  changes update in about a second. The preview uses its own KiCad settings
  folder in which all model types (SMD, through-hole, virtual) are shown,
  whatever your 3D viewer is set to.
* Projects reference parts as `Plus381_<Category>:<Name>`. KiCad also keeps a
  copy of every part inside the schematic/board, so editing a part never
  changes existing boards until someone runs *Update from Library*.

Settings and logs live in `%APPDATA%\plus381` (Windows),
`~/Library/Application Support/plus381` (macOS) or `~/.config/plus381` (Linux).

## Uninstall

Uninstall the
plugin in the Plugin and Content Manager, and remove the `Plus381_*` entries
in **Preferences → Manage Symbol/Footprint Libraries**.

## Development

```text
plus381-tools/
  plugin.json            KiCad API plugin manifest (3 toolbar actions)
  pcm.json               Plugin and Content Manager metadata
  import_part.py, publish.py, library_status.py   entry points
  plus381/partsync.py    git sync, background check for new parts, KiCad settings (standard library only)
  plus381/badge.py       red dot on the toolbar buttons (runs inside KiCad)
  plus381/start.py       starts an action; shows + logs startup errors
  plus381/svgdraw.py     draws kicad-cli's SVGs (KiCad's Windows wxPython has no wx.svg)
  plus381/importer.py    vendor zip -> library part
  plus381/publish.py     commit / push / conflict handling
  plus381/github.py      in-plugin GitHub login (device flow), fork + pull request
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

### GitHub login (OAuth App), one-time setup by an org owner

1. github.com/organizations/plus381-robotics → **Settings → Developer settings
   → OAuth Apps → New OAuth App**.
   * Application name: `Plus381 Tools`
   * Homepage URL: `https://github.com/plus381-robotics/kicad-plugins`
   * Authorization callback URL: `https://github.com/plus381-robotics/kicad-plugins`
     (not used by device login, but required by the form)
2. Create it, then tick **Enable Device Flow** and save.
3. Copy the **Client ID** into `CLIENT_ID` in `plus381-tools/plus381/github.py`,
   bump `__version__`, and push. The Client ID is public by design, and no
   client secret is used or needed.

The plugin asks for the `public_repo` scope: enough to push to `kicad-parts`,
fork it and open pull requests. Until `CLIENT_ID` is set, Publish falls back to
whatever login Git already has (e.g. Git Credential Manager on Windows).
For tests, `PLUS381_GITHUB_CLIENT_ID`, `PLUS381_GITHUB_WEB` and
`PLUS381_GITHUB_API` override the client ID and GitHub addresses.
