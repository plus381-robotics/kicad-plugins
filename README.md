# Plus381 KiCad plugins

KiCad 10 plugins for the Plus381 Robotics team.

| Plugin | What it does |
|---|---|
| [`plus381-tools`](plus381-tools/) | Connects KiCad to the team parts library ([kicad-parts](https://github.com/plus381-robotics/kicad-parts)): import vendor parts, publish them, keep everyone's library up to date automatically |

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

## Daily use

| Task | How |
|---|---|
| Get new parts | Nothing to do. While KiCad is open, the library updates itself every minute (and right away whenever you click a Plus381 button). Reopen the footprint/symbol chooser to see new parts. |
| Add a part | Download the part's KiCad zip from SnapEDA / Mouser / UltraLibrarian / DigiKey → **Import part** → pick the zip and a category → check the **3D preview** (fix offset/rotation; **Vendor values** undoes your changes) → **Import** → **Publish**. |
| Fix or improve a part | Edit it in the Symbol / Footprint Editor (it's already in the `Plus381_*` libraries), save, then **Publish**. |
| See what's going on | **Library status**: last sync, background sync on/off, unpublished changes, library check, re-run setup. |

If someone else changed the same part at the same time, the plugin asks
whose version to keep and backs up the other one.

## Logging in (only for publishing)

The first time you click **Publish**, the plugin asks you to log in:

1. A window shows a code like `WDJB-MJHT` and your browser opens
   github.com/login/device.
2. Enter the code and click **Authorize**. KiCad continues by itself.

The login is kept in your computer's credential storage (Windows Credential
Manager, macOS Keychain, or a private file in the plugin's settings folder on
Linux) and reused from then on. **Library status** shows who is logged in and
has **Log in to GitHub** / **Log out** buttons.

* **Members of plus381-robotics** publish straight into the team library.
* **Anyone else** (alumni, other teams) can publish too: the plugin creates
  their own copy (fork) of `kicad-parts` and opens a **pull request**. A member
  reviews and merges it, then everyone gets the part. Publishing again
  before it's merged updates the same pull request. The parts stay usable on
  the author's computer while they wait for review.

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
  syncs while KiCad is open: when KiCad is closed it exits immediately without
  going online, so new parts arrive within a minute of opening KiCad. (KiCad
  can't start plugins by itself on startup, which is why a scheduler job is
  used.) It only fast-forwards: it never overwrites files you are editing and never
  publishes on its own, except for retrying a Publish that failed while
  you were offline. It also re-registers new categories and repairs the path
  variable if needed.
* **Import** converts the vendor files to the current KiCad format with
  `kicad-cli`. It stores the symbol and footprint as one file each (so two
  people adding parts never conflict) and links symbol → footprint → 3D
  model. It reuses an identical 3D model instead of storing a second copy, and
  keeps the vendor's 3D offset and rotation. The 3D preview (3D, front, right
  and top views) is rendered by `kicad-cli pcb render` on a tiny temporary
  board holding just the part, so it shows exactly what KiCad will show. The
  first render reads the STEP file and takes a few seconds; after that,
  changes update in about a second.
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
