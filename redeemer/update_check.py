"""Update visibility — is there newer code/a newer release than what's running?

Checked once at startup (in the background) and every few hours after; the
result feeds the update banner in the UI. Silencing is the user's choice and
lives in app settings, not here — this module only answers "what's newest,
and are we behind it?".

Two update channels, matching how each install actually updates:
  release — frozen builds (the packaged exe). Compares the version tag baked
            in at build time (static/version.txt) against the newest GitHub
            release, so users are only notified when there's a new download.
  branch  — running from source. Compares the local git HEAD (or, for zip
            downloads, source mtimes) against the head of the main branch,
            so a plain `git push` is visible.

Emergency releases: if the repo's update_notice.json has emergency=true, the
UI shows its title/message as an urgent banner that overrides any silence
preference. See update_notice.json in the repo root for how to flip it.

Everything network-related is best-effort: offline or rate-limited simply
reports ok=False and the app carries on.
"""
import calendar
import glob
import json
import os
import subprocess
import sys
import threading
import time

import requests

REPO = "Slagathore/HumbleRedeemer"
BRANCH = "main"
REPO_URL = f"https://github.com/{REPO}"
DOWNLOAD_URL = f"{REPO_URL}/releases/latest"
COMMITS_URL = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
COMPARE_URL = f"https://api.github.com/repos/{REPO}/compare/{{base}}...{BRANCH}"
RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
NOTICE_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/update_notice.json"

CHECK_TTL = 6 * 3600          # re-check at most this often unless forced
_TIMEOUT = 10
_HEADERS = {"User-Agent": "HumbleRedeemer-update-check",
            "Accept": "application/vnd.github+json"}
# mtimes from a GitHub zip equal the commit time, so a fresh download must not
# read as "behind" the very commit it contains
_MTIME_GRACE = 90

FROZEN = getattr(sys, "frozen", False)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_lock = threading.Lock()
_cache: "dict | None" = None


def build_tag():
    """The release tag stamped into a packaged build by the CI workflow
    (static/version.txt), or None for source runs / hand-rolled builds."""
    if not FROZEN:
        return None
    try:
        path = os.path.join(getattr(sys, "_MEIPASS", "."), "static", "version.txt")
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _git(*args):
    """Run git in the repo root; None on any failure (no git, not a clone...)."""
    try:
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        out = subprocess.run(("git",) + args, cwd=_ROOT, capture_output=True,
                             text=True, timeout=5, creationflags=flags)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def local_version():
    """Source installs: (sha_or_None, epoch) identifying the running code."""
    line = _git("show", "-s", "--format=%H %ct", "HEAD")
    if line and " " in line:
        sha, epoch = line.split()[:2]
        return sha, int(epoch)
    files = [os.path.join(_ROOT, "app.py"),
             os.path.join(_ROOT, "static", "index.html")]
    files += glob.glob(os.path.join(_ROOT, "redeemer", "*.py"))
    stamps = [os.path.getmtime(f) for f in files if os.path.exists(f)]
    return None, (max(stamps) if stamps else 0)


def _iso_epoch(date):
    try:
        return calendar.timegm(time.strptime(date, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return 0


def _fetch_branch_head():
    """(sha, epoch, iso_date, first_line_of_message) of the branch head."""
    r = requests.get(COMMITS_URL, headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    commit = data.get("commit", {})
    date = (commit.get("committer") or commit.get("author") or {}).get("date", "")
    msg = (commit.get("message") or "").splitlines()
    return data.get("sha", ""), _iso_epoch(date), date, (msg[0][:120] if msg else "")


def _fetch_latest_release():
    """(tag, epoch, iso_date, release_name, html_url) of the newest release."""
    r = requests.get(RELEASE_URL, headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    date = data.get("published_at", "")
    name = (data.get("name") or data.get("tag_name") or "")[:120]
    return (data.get("tag_name", ""), _iso_epoch(date), date, name,
            data.get("html_url", ""))


def _compare(local_sha):
    """(commits_behind, up to 5 newest change titles) via GitHub's compare API.
    Raises if the local sha is unknown to GitHub (e.g. local-only commits)."""
    r = requests.get(COMPARE_URL.format(base=local_sha),
                     headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    msgs = [(c.get("commit", {}).get("message") or "").splitlines()[0][:120]
            for c in data.get("commits", [])[-5:]]
    msgs.reverse()  # newest first
    return int(data.get("ahead_by", 0)), msgs


def _fetch_notice():
    """The repo's update_notice.json, sanitized — or None if absent/invalid."""
    try:
        r = requests.get(NOTICE_URL, headers={"User-Agent": _HEADERS["User-Agent"]},
                         timeout=_TIMEOUT)
        if r.status_code != 200:
            return None
        n = json.loads(r.text)
        if not isinstance(n, dict):
            return None
        return {"emergency": bool(n.get("emergency")),
                "id": str(n.get("id") or "")[:80],
                "title": str(n.get("title") or "")[:200],
                "message": str(n.get("message") or "")[:1000]}
    except Exception:
        return None


def _check():
    result = {
        "ok": False, "error": "",
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "channel": "release" if FROZEN else "branch",
        "update_available": False, "behind_by": None,
        "local_sha": None, "remote_sha": "", "remote_date": "",
        "remote_message": "", "recent_changes": [], "emergency": None,
        "download_url": DOWNLOAD_URL,
    }

    if FROZEN:
        # Packaged build: only a newer *release* is actionable.
        tag = build_tag()
        result["local_sha"] = tag
        try:
            r_tag, r_epoch, r_date, r_name, r_url = _fetch_latest_release()
        except Exception as e:
            result["error"] = f"{e.__class__.__name__}: {e}"
            return result
        result.update(ok=True, remote_sha=r_tag, remote_date=r_date,
                      remote_message=r_name,
                      download_url=r_url or DOWNLOAD_URL)
        exe_epoch = os.path.getmtime(sys.executable)
        if tag:
            # tag differs AND the release is newer than this build — a locally
            # built pre-release exe must not count as "behind"
            result["update_available"] = bool(
                r_tag and r_tag != tag and r_epoch > exe_epoch)
        else:
            result["update_available"] = (
                r_epoch > 0 and r_epoch > exe_epoch + _MTIME_GRACE)
        result["emergency"] = _fetch_notice()
        return result

    # Source install: pushes to the branch are the update signal.
    local_sha, local_epoch = local_version()
    result["local_sha"] = local_sha
    try:
        remote_sha, remote_epoch, remote_date, remote_msg = _fetch_branch_head()
    except Exception as e:
        result["error"] = f"{e.__class__.__name__}: {e}"
        return result
    result.update(ok=True, remote_sha=remote_sha, remote_date=remote_date,
                  remote_message=remote_msg)

    if local_sha and local_sha == remote_sha:
        pass  # exactly up to date
    elif local_sha:
        try:
            behind, msgs = _compare(local_sha)
            result["behind_by"] = behind
            result["recent_changes"] = msgs
            result["update_available"] = behind > 0
        except Exception:
            # sha not on GitHub (local commits/rebase) — fall back to dates so
            # a dev copy that's AHEAD doesn't nag about "updates"
            result["update_available"] = remote_epoch > local_epoch
    else:
        result["update_available"] = (
            remote_epoch > 0 and remote_epoch > local_epoch + _MTIME_GRACE)

    result["emergency"] = _fetch_notice()
    return result


def get_status(force=False):
    """Cached check result; hits the network only when stale or forced."""
    global _cache
    with _lock:
        if (not force and _cache is not None
                and time.time() - _cache["_at"] < CHECK_TTL):
            return {k: v for k, v in _cache.items() if k != "_at"}
    fresh = _check()
    with _lock:
        _cache = {**fresh, "_at": time.time()}
    return fresh
