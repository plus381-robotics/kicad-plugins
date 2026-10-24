"""Install/remove the background job that keeps the parts library up to date.

The job runs partsync.py every minute through the operating system's own
scheduler. partsync.py only syncs while KiCad is open (KiCad itself cannot
start plugins in the background), so new parts arrive within a minute of
opening KiCad and keep arriving while it runs:
  Windows: Task Scheduler (hidden window via wscript)
  macOS:   a LaunchAgent
  Linux:   a systemd user timer, or cron when systemd is not available
"""

import os
import shutil
import subprocess
from pathlib import Path

from . import partsync
from .partsync import IS_MAC, IS_WIN, _hidden, app_dir

TASK = "Plus381 Parts Sync"
LABEL = "com.plus381.partsync"
UNIT = "plus381-partsync"
CRON_MARK = "# plus381-partsync"


def _script():
    """Copy partsync.py to the config folder so the job keeps working when the
    plugin is updated or KiCad recreates the plugin's Python environment."""
    dest = app_dir() / "partsync.py"
    shutil.copyfile(Path(partsync.__file__), dest)
    return dest


def refresh_script():
    """Keep the scheduler's copy of partsync.py in step with the installed plugin."""
    dest = app_dir() / "partsync.py"
    if dest.exists() and dest.read_bytes() != Path(partsync.__file__).read_bytes():
        _script()


def _run(*cmd, check=True, input=None):
    r = subprocess.run(list(cmd), capture_output=True, text=True, input=input, **_hidden())
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip()}")
    return r


def install(interval_minutes=1):
    py = partsync.base_python()
    script = _script()
    cfg = partsync.load_config()
    cfg["python"] = py
    partsync.save_config(cfg)

    if IS_WIN:
        vbs = app_dir() / "partsync.vbs"
        vbs.write_text(
            'CreateObject("WScript.Shell").Run """{}"" ""{}"" sync --quiet", 0, False\r\n'.format(py, script),
            encoding="utf-8")
        _run("schtasks", "/Create", "/F", "/TN", TASK, "/SC", "MINUTE", "/MO", str(interval_minutes),
             "/TR", f'wscript.exe "{vbs}"')
        return f"Windows Task Scheduler task '{TASK}'"

    if IS_MAC:
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        log = app_dir() / "partsync.log"
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{py}</string><string>{script}</string><string>sync</string><string>--quiet</string></array>
  <key>StartInterval</key><integer>{interval_minutes * 60}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
""", encoding="utf-8")
        domain = f"gui/{os.getuid()}"
        _run("launchctl", "bootout", domain, str(plist), check=False)
        _run("launchctl", "bootstrap", domain, str(plist))
        return f"LaunchAgent {LABEL}"

    if _systemd_ok():
        d = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{UNIT}.service").write_text(f"""[Unit]
Description=Plus381 KiCad parts library sync

[Service]
Type=oneshot
ExecStart="{py}" "{script}" sync --quiet
""", encoding="utf-8")
        (d / f"{UNIT}.timer").write_text(f"""[Unit]
Description=Sync Plus381 KiCad parts library every {interval_minutes} min

[Timer]
OnBootSec=1min
OnUnitActiveSec={interval_minutes}min

[Install]
WantedBy=timers.target
""", encoding="utf-8")
        _run("systemctl", "--user", "daemon-reload")
        _run("systemctl", "--user", "enable", "--now", f"{UNIT}.timer")
        return f"systemd user timer {UNIT}.timer"

    if shutil.which("crontab"):
        line = f'*/{interval_minutes} * * * * "{py}" "{script}" sync --quiet {CRON_MARK}'
        _run("crontab", "-", input=_cron_without_us() + line + "\n")
        return "cron job"

    raise RuntimeError("No scheduler found (systemd or cron). Use 'Sync now' instead.")


def uninstall():
    if IS_WIN:
        _run("schtasks", "/Delete", "/F", "/TN", TASK, check=False)
    elif IS_MAC:
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        _run("launchctl", "bootout", f"gui/{os.getuid()}", str(plist), check=False)
        if plist.exists():
            plist.unlink()
    else:
        if shutil.which("systemctl"):
            _run("systemctl", "--user", "disable", "--now", f"{UNIT}.timer", check=False)
            d = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
            for f in (d / f"{UNIT}.service", d / f"{UNIT}.timer"):
                if f.exists():
                    f.unlink()
        if shutil.which("crontab"):
            _run("crontab", "-", input=_cron_without_us(), check=False)


def installed():
    if IS_WIN:
        return _run("schtasks", "/Query", "/TN", TASK, check=False).returncode == 0
    if IS_MAC:
        return (Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist").exists()
    if shutil.which("systemctl") and _run("systemctl", "--user", "is-enabled", f"{UNIT}.timer",
                                          check=False).returncode == 0:
        return True
    return shutil.which("crontab") is not None and CRON_MARK in _run("crontab", "-l", check=False).stdout


def _systemd_ok():
    return shutil.which("systemctl") is not None and \
        _run("systemctl", "--user", "show-environment", check=False).returncode == 0


def _cron_without_us():
    cur = _run("crontab", "-l", check=False).stdout
    return "".join(l + "\n" for l in cur.splitlines() if CRON_MARK not in l)


def start_configure_after_exit():
    """KiCad overwrites its settings when it closes, so the path variable is
    written by a small background process right after KiCad exits."""
    py = partsync.base_python()
    script = _script()
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if IS_WIN:
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([py, str(script), "configure-kicad"], **kwargs)
