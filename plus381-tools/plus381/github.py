"""GitHub login from inside KiCad (OAuth device flow) and the few API calls
the plugin needs: who am I, may I push, fork, open a pull request.

The login token is handed to Git's own credential storage (Windows
Credential Manager, macOS Keychain, or a private file on Linux), so plain
`git push` uses it and the user never types a password.
"""

import json
import os
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from . import partsync
from .partsync import _hidden, app_dir

# OAuth App "Plus381 Tools" owned by the plus381-robotics organisation, with
# Device Flow enabled. The client ID is public by design (no secret is used).
CLIENT_ID = ""
SCOPE = "public_repo"
OWNER, REPO = "plus381-robotics", "kicad-parts"

# Overridable for tests against a fake server.
WEB = os.environ.get("PLUS381_GITHUB_WEB", "https://github.com").rstrip("/")
API = os.environ.get("PLUS381_GITHUB_API", "https://api.github.com").rstrip("/")
HOST = urllib.parse.urlparse(WEB).netloc


class GitHubError(Exception):
    pass


def client_id():
    return (os.environ.get("PLUS381_GITHUB_CLIENT_ID") or partsync.load_config().get("github_client_id")
            or CLIENT_ID)


# --------------------------------------------------------------------------
# HTTP (urllib, falling back to curl where Python has no CA certificates)
# --------------------------------------------------------------------------

def _request(method, url, data=None, token=None, form=False):
    headers = {"Accept": "application/json", "User-Agent": "plus381-tools",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, _json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _json(e.read())
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError) and shutil.which("curl"):
            return _curl(method, url, headers, body)
        raise GitHubError(f"Could not reach GitHub: {e.reason}")


def _curl(method, url, headers, body):
    cmd = ["curl", "-sS", "-X", method, "-w", "\n%{http_code}", url]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    if body is not None:
        cmd += ["--data-binary", "@-"]
    r = subprocess.run(cmd, input=body, capture_output=True, **_hidden())
    if r.returncode != 0:
        raise GitHubError("Could not reach GitHub: " + r.stderr.decode(errors="replace").strip())
    out, _, code = r.stdout.decode(errors="replace").rpartition("\n")
    return int(code), _json(out.encode())


def _json(raw):
    try:
        return json.loads(raw.decode() or "{}")
    except ValueError:
        return {}


def api(method, path, token, data=None):
    status, body = _request(method, API + path, data, token)
    if status >= 400:
        raise GitHubError(f"GitHub {method} {path}: {status} {body.get('message', '')}".strip())
    return body


# --------------------------------------------------------------------------
# Device flow login
# --------------------------------------------------------------------------

def start_login():
    """Returns {"user_code", "verification_uri", "device_code", "interval", "expires_in"}."""
    cid = client_id()
    if not cid:
        raise GitHubError("Logging in from KiCad is not configured yet (no OAuth app client ID). "
                          "Ask whoever maintains Plus381 Tools.")
    status, body = _request("POST", WEB + "/login/device/code", {"client_id": cid, "scope": SCOPE}, form=True)
    if status >= 400 or "device_code" not in body:
        raise GitHubError("GitHub refused the login request: " +
                          (body.get("error_description") or body.get("error") or str(status)))
    return body


def wait_for_login(start, cancelled=lambda: False):
    """Poll until the user approved the code in the browser. Returns the token."""
    interval = int(start.get("interval", 5))
    deadline = time.time() + int(start.get("expires_in", 900))
    while time.time() < deadline:
        for _ in range(interval * 10):
            if cancelled():
                raise GitHubError("Login cancelled.")
            time.sleep(0.1)
        status, body = _request("POST", WEB + "/login/oauth/access_token", {
            "client_id": client_id(), "device_code": start["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, form=True)
        if body.get("access_token"):
            return body["access_token"]
        err = body.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval = int(body.get("interval", interval + 5))
            continue
        if err == "access_denied":
            raise GitHubError("The login was cancelled on GitHub.")
        if err == "expired_token":
            break
        raise GitHubError("GitHub login failed: " + (body.get("error_description") or err or str(status)))
    raise GitHubError("The code expired. Click Log in to try again.")


# --------------------------------------------------------------------------
# Token storage in Git's credential system
# --------------------------------------------------------------------------

def _git_cred(cfg, action, fields):
    exe = cfg.get("git") or partsync.find_git()
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    text = "".join(f"{k}={v}\n" for k, v in fields.items()) + "\n"
    r = subprocess.run([exe, "-C", cfg["parts_dir"], "credential", action], input=text, capture_output=True,
                       text=True, env=env, timeout=60, **_hidden())
    out = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


def _ensure_helper(cfg):
    """Use the computer's credential manager when Git has one; otherwise a
    private file in the plugin's settings folder (Linux without a keyring)."""
    exe = cfg.get("git") or partsync.find_git()
    helper = subprocess.run([exe, "-C", cfg["parts_dir"], "config", "--get-all", "credential.helper"],
                            capture_output=True, text=True, **_hidden()).stdout.strip()
    if helper:
        return
    store = app_dir() / "git-credentials"
    store.touch(exist_ok=True)
    try:
        os.chmod(store, 0o600)
    except OSError:
        pass
    partsync.git(cfg, "config", "--local", "credential.helper", f'store --file="{store.as_posix()}"')


def save_token(cfg, login, token):
    _ensure_helper(cfg)
    _git_cred(cfg, "approve", {"protocol": "https", "host": HOST, "username": login, "password": token})
    cfg = partsync.load_config()
    cfg["github_login"] = login
    partsync.save_config(cfg)


def get_token(cfg):
    login = partsync.load_config().get("github_login")
    if not login:
        return None
    out = _git_cred(cfg, "fill", {"protocol": "https", "host": HOST, "username": login})
    return out.get("password") or None


def logout(cfg):
    login = partsync.load_config().get("github_login")
    token = get_token(cfg)
    if login and token:
        _git_cred(cfg, "reject", {"protocol": "https", "host": HOST, "username": login, "password": token})
    cfg = partsync.load_config()
    for k in ("github_login", "publish_mode"):
        cfg.pop(k, None)
    partsync.save_config(cfg)


# --------------------------------------------------------------------------
# Account and permissions
# --------------------------------------------------------------------------

def whoami(token):
    u = api("GET", "/user", token)
    return {"login": u["login"], "name": u.get("name") or u["login"],
            "email": f"{u['id']}+{u['login']}@users.noreply.github.com"}


def can_push(token):
    repo = api("GET", f"/repos/{OWNER}/{REPO}", token)
    return bool(repo.get("permissions", {}).get("push"))


def after_login(cfg, token):
    """Remember who is logged in and whether they publish directly or via
    pull requests. Fills in the Git name/e-mail if they are missing."""
    me = whoami(token)
    save_token(cfg, me["login"], token)
    mode = "direct" if can_push(token) else "pr"
    c = partsync.load_config()
    c["publish_mode"] = mode
    partsync.save_config(c)
    if not partsync.git(cfg, "config", "user.email", check=False).stdout.strip():
        partsync.git(cfg, "config", "user.name", me["name"])
        partsync.git(cfg, "config", "user.email", me["email"])
    return me, mode


# --------------------------------------------------------------------------
# Pull request fallback for people without write access
# --------------------------------------------------------------------------

def ensure_fork(token, login):
    """The user's fork of kicad-parts (created on first use)."""
    status, body = _request("GET", f"{API}/repos/{login}/{REPO}", token=token)
    if status == 200 and body.get("fork"):
        return body
    api("POST", f"/repos/{OWNER}/{REPO}/forks", token, {"default_branch_only": True})
    for _ in range(60):  # GitHub creates forks asynchronously
        status, body = _request("GET", f"{API}/repos/{login}/{REPO}", token=token)
        if status == 200:
            return body
        time.sleep(2)
    raise GitHubError("GitHub is still creating your copy (fork) of the library. Try Publish again in a minute.")


def open_pull_request(cfg, token, title, body_text):
    """Push the unpublished commits to the user's fork and open (or update)
    one pull request for them. Returns the pull request's web URL."""
    login = partsync.load_config()["github_login"]
    fork = ensure_fork(token, login)
    branch = f"parts/{login}"
    url = fork.get("clone_url") or f"{WEB}/{login}/{REPO}.git"
    # The branch only ever holds this user's pending parts on top of main.
    partsync.git(cfg, "push", "--quiet", "--force", url, f"HEAD:refs/heads/{branch}")
    status, existing = _request("GET", f"{API}/repos/{OWNER}/{REPO}/pulls?state=open&head={login}:{branch}",
                                token=token)
    if status == 200 and isinstance(existing, list) and existing:
        return existing[0]["html_url"], False
    pr = api("POST", f"/repos/{OWNER}/{REPO}/pulls", token,
             {"title": title, "head": f"{login}:{branch}", "base": partsync.BRANCH, "body": body_text,
              "maintainer_can_modify": True})
    return pr["html_url"], True


def device_page(start):
    return start.get("verification_uri") or WEB + "/login/device"

