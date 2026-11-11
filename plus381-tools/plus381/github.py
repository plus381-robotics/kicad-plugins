"""GitHub login from inside KiCad (OAuth device flow) and the few API calls
the plugin needs: who am I, may I push, fork, open a pull request.

The login token is handed to Git's own credential storage (Windows
Credential Manager, macOS Keychain, or a private file on Linux), so plain
`git push` uses it and the user never types a password.
"""

import http.client
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
from .partsync import _hidden

# OAuth App "Plus381 Tools" owned by the plus381-robotics organisation, with
# Device Flow enabled. The client ID is public by design (no secret is used).
CLIENT_ID = "Ov23liVILoeUcEa0Nh1w"
SCOPE = "public_repo"



def repo_slug(cfg=None):
    """(owner, name) of the library repository on GitHub."""
    from urllib.parse import urlparse
    path = urlparse(partsync.repo_url(cfg or partsync.load_config())).path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    owner, _, name = path.partition("/")
    return owner, name

# Overridable for tests against a fake server.
WEB = partsync.GITHUB_WEB
API = os.environ.get("PLUS381_GITHUB_API", "https://api.github.com").rstrip("/")
HOST = urllib.parse.urlparse(WEB).netloc


class GitHubError(Exception):
    pass


class NetworkError(GitHubError):
    """GitHub could not be reached (offline, timeout, connection dropped)."""


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
        raise NetworkError(f"Could not reach GitHub: {e.reason}")
    except (OSError, http.client.HTTPException) as e:
        # A timeout or a dropped connection while reading the answer is not
        # wrapped in URLError by urllib.
        raise NetworkError(f"Could not reach GitHub: {e or type(e).__name__}")


def _curl(method, url, headers, body):
    cmd = ["curl", "-sS", "-X", method, "-w", "\n%{http_code}", url]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    if body is not None:
        cmd += ["--data-binary", "@-"]
    try:
        r = subprocess.run(cmd, input=body, capture_output=True, timeout=60, **_hidden())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise NetworkError(f"Could not reach GitHub: {e}")
    if r.returncode != 0:
        raise NetworkError("Could not reach GitHub: " + r.stderr.decode(errors="replace").strip())
    out, _, code = r.stdout.decode(errors="replace").rpartition("\n")
    try:
        return int(code), _json(out.encode())
    except ValueError:
        raise NetworkError("Could not reach GitHub: unexpected answer from curl")


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
    offline = None
    while time.time() < deadline:
        for _ in range(interval * 10):
            if cancelled():
                raise GitHubError("Login cancelled.")
            time.sleep(0.1)
        try:
            status, body = _request("POST", WEB + "/login/oauth/access_token", {
                "client_id": client_id(), "device_code": start["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, form=True)
        except NetworkError as e:
            offline = e  # a hiccup in the connection: keep waiting, the code is still valid
            continue
        offline = None
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
    if offline:
        raise offline
    raise GitHubError("The code expired. Click Log in to try again.")


# --------------------------------------------------------------------------
# Token storage in Git's credential system
# --------------------------------------------------------------------------

def _git_cred(cfg, action, fields):
    exe = partsync.git_exe(cfg)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    text = "".join(f"{k}={v}\n" for k, v in fields.items()) + "\n"
    r = subprocess.run([exe, "-C", cfg["parts_dir"], "credential", action], input=text, capture_output=True,
                       text=True, env=env, timeout=60, **_hidden())
    out = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


def save_token(cfg, login, token):
    partsync.save_token(token)
    cfg = partsync.load_config()
    cfg["github_login"] = login
    partsync.save_config(cfg)


def get_token(cfg):
    token = partsync.load_token()
    if token:
        return token
    # Logins from plugin versions before 0.6 live in Git's credential store:
    # move them to the plugin's own store.
    login = partsync.load_config().get("github_login")
    if not login:
        return None
    token = _git_cred(cfg, "fill", {"protocol": "https", "host": HOST, "username": login}).get("password")
    if token:
        partsync.save_token(token)
    return token or None


def logout(cfg):
    login = partsync.load_config().get("github_login")
    token = get_token(cfg)
    partsync.delete_token()
    if login and token:  # also remove a login left by older versions
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
    owner, name = repo_slug()
    repo = api("GET", f"/repos/{owner}/{name}", token)
    return bool(repo.get("permissions", {}).get("push"))


def publish_mode(token):
    """"direct" for people who may write to the library, "pr" (pull requests)
    for everyone else. Asked from GitHub each time and remembered, because
    it changes when someone joins or leaves the team."""
    mode = "direct" if can_push(token) else "pr"
    c = partsync.load_config()
    if c.get("publish_mode") != mode:
        c["publish_mode"] = mode
        partsync.save_config(c)
    return mode


def after_login(cfg, token):
    """Remember who is logged in and whether they publish directly or via
    pull requests. Fills in the Git name/e-mail if they are missing."""
    me = whoami(token)
    save_token(cfg, me["login"], token)
    mode = publish_mode(token)
    if not partsync.git(cfg, "config", "user.email", check=False).stdout.strip():
        partsync.git(cfg, "config", "user.name", me["name"])
        partsync.git(cfg, "config", "user.email", me["email"])
    return me, mode


# --------------------------------------------------------------------------
# Pull request fallback for people without write access
# --------------------------------------------------------------------------

def ensure_fork(token, login):
    """The user's fork of the library repository (created on first use)."""
    owner, name = repo_slug()
    library = f"{owner}/{name}".lower()

    def is_fork(repo):
        origins = {(repo.get(k) or {}).get("full_name", "").lower() for k in ("parent", "source")}
        return bool(repo.get("fork")) and library in origins

    status, body = _request("GET", f"{API}/repos/{login}/{name}", token=token)
    if status == 200 and is_fork(body):
        return body
    # Returns the existing fork if there is one under another name; a new
    # fork gets another name when the user already has a repository called
    # like the library. Never use a repository that isn't a fork of it.
    created = api("POST", f"/repos/{owner}/{name}/forks", token, {"default_branch_only": True})
    full = created.get("full_name") or f"{login}/{name}"
    for _ in range(60):  # GitHub creates forks asynchronously
        status, body = _request("GET", f"{API}/repos/{full}", token=token)
        if status == 200 and is_fork(body):
            return body
        time.sleep(2)
    raise GitHubError("GitHub is still creating your copy (fork) of the library. Try Publish again in a minute.")


def open_pull_request(cfg, token, title, body_text):
    """Push the unpublished commits to the user's fork and open (or update)
    one pull request for them. Returns the pull request's web URL."""
    login = partsync.load_config()["github_login"]
    fork = ensure_fork(token, login)
    branch = f"parts/{login}"
    owner, name = repo_slug()
    url = fork.get("clone_url") or f"{WEB}/{login}/{name}.git"
    # The branch only ever holds this user's pending parts on top of main.
    partsync.git(cfg, "push", "--quiet", "--force", url, f"HEAD:refs/heads/{branch}")
    status, existing = _request("GET", f"{API}/repos/{owner}/{name}/pulls?state=open&head={login}:{branch}",
                                token=token)
    if status == 200 and isinstance(existing, list) and existing:
        return existing[0]["html_url"], False
    pr = api("POST", f"/repos/{owner}/{name}/pulls", token,
             {"title": title, "head": f"{login}:{branch}", "base": partsync.BRANCH, "body": body_text,
              "maintainer_can_modify": True})
    return pr["html_url"], True


def device_page(start):
    return start.get("verification_uri") or WEB + "/login/device"

