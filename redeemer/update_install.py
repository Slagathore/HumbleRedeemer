"""Installing an update, not just pointing at one.

update_check.py answers "is there something newer?". This module does the
rest for the packaged Windows build: fetch the release's installer, prove it
is ours, run it, and get out of the way so it can replace the exe.

The proof step is the whole point. An update channel that downloads and runs
an exe without checking it is a remote code execution hole with a progress
bar, so nothing here executes anything that hasn't passed:

  1. SHA256 against the release's published SHA256SUMS.txt, when the release
     publishes one (build_release.ps1 produces it).
  2. Authenticode: Windows itself must call the signature Valid, and the
     signing certificate must be the project's publisher. A download that is
     unsigned, tampered with, or signed by somebody else is deleted, not run.

Failing either check ends the run in state "failed" with the reason. There is
no flag, setting or fallback that skips verification.

Source checkouts get nothing binary: supported() tells them to git pull.
"""
import hashlib
import os
import re
import subprocess
import sys
import threading

import requests

from redeemer import paths
from redeemer.update_check import (DOWNLOAD_URL, RELEASE_URL, _HEADERS,
                                   _TIMEOUT)

# The certificate the Windows builds are signed with (Azure Artifact Signing,
# see SIGNING.md). This is a check, not a claim: if a download's signer
# doesn't say this, the download is refused.
EXPECTED_PUBLISHER = "Charles Chambers"

# Release asset names. The Inno Setup installer is the only asset that can
# actually install itself; the bare exe and the zip are manual downloads.
_INSTALLER_RE = re.compile(r"-setup\.exe$", re.I)
_CHECKSUM_NAMES = ("sha256sums.txt", "sha256sums")

_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200

GIT_PULL_HINT = ("You're running from source, so there's nothing to install: "
                 "run `git pull` in the repo and restart the app.")

_lock = threading.Lock()
_thread = None
_state = {
    "state": "idle",       # idle|checking|downloading|verifying|ready|installing|failed
    "message": "",
    "progress": 0,         # 0-100 while downloading
    "asset": "",
    "path": "",
    "verified": "",        # what actually passed, only set once it has
    "expected_sha": None,  # remembered so launch() can re-verify the exact file
    "error": "",
    "download_url": DOWNLOAD_URL,
}


class VerificationError(Exception):
    """The download is not provably ours. It does not get run."""


# ---------------- state (what the UI is allowed to say) ----------------

def status():
    with _lock:
        return dict(_state)


def _set(**fields):
    with _lock:
        _state.update(fields)


def _reset():
    _set(state="idle", message="", progress=0, asset="", path="",
         verified="", expected_sha=None, error="", download_url=DOWNLOAD_URL)


def _fail(message):
    _set(state="failed", error=message, message=message, verified="", path="")


def supported():
    """(can_install, reason). Reason is what the UI shows when it can't."""
    if not paths.frozen():
        return False, GIT_PULL_HINT
    if os.name != "nt" or sys.platform != "win32":
        return False, ("Automatic install is Windows only. Download the new "
                       "build from the releases page.")
    return True, ""


# ---------------- the release's assets ----------------

def _fetch_release():
    r = requests.get(RELEASE_URL, headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def pick_installer(release):
    """The Inno Setup installer asset, or None if this release doesn't ship
    one (in which case we say so instead of running something else)."""
    for asset in release.get("assets") or []:
        name = asset.get("name") or ""
        if _INSTALLER_RE.search(name):
            return asset
    return None


def pick_checksums(release):
    for asset in release.get("assets") or []:
        if (asset.get("name") or "").lower() in _CHECKSUM_NAMES:
            return asset
    return None


def parse_checksums(text):
    """{filename: sha256} from a `sha256sum`-style listing. Anything that
    isn't a 64-hex digest plus a name is ignored."""
    sums = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        name = os.path.basename(parts[-1].lstrip("*").strip())
        if name:
            sums[name] = digest
    return sums


# ---------------- verification ----------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def authenticode(path):
    """(status, signer_subject) as Windows reports them. Raises on any
    platform or environment where we can't ask, because "couldn't check" must
    never read as "checked out fine"."""
    if os.name != "nt":
        raise VerificationError(
            "Signature verification needs Windows, so this download can't be "
            "proved genuine here.")
    quoted = "'" + os.path.abspath(path).replace("'", "''") + "'"
    script = (f"$s = Get-AuthenticodeSignature -LiteralPath {quoted}; "
              "Write-Output $s.Status; "
              "if ($s.SignerCertificate) { Write-Output $s.SignerCertificate.Subject }")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=90,
            creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        raise VerificationError(
            f"Couldn't ask Windows to check the signature ({e.__class__.__name__}).")
    if out.returncode != 0:
        raise VerificationError(
            "Couldn't ask Windows to check the signature "
            f"(exit {out.returncode}).")
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    if not lines:
        raise VerificationError("Windows returned no signature status.")
    return lines[0], (lines[1] if len(lines) > 1 else "")


def subject_cn(subject):
    """The CN value out of an X.500 subject string like
    ``CN=Charles Chambers, O=Charles Chambers, C=US``. Returns None if there
    is no CN. Respects backslash-escaped commas inside a value, so this is the
    actual Common Name, not "the text CN= happens to be followed by".

    This is deliberately anchored: an attacker who puts our name in their O,
    OU or anywhere else in the subject must not slip past the publisher check.
    """
    if not subject:
        return None
    m = re.search(r"(?:^|,)\s*CN=", subject, re.I)
    if not m:
        return None
    rest = subject[m.end():]
    out = []
    i = 0
    while i < len(rest):
        ch = rest[i]
        if ch == "\\" and i + 1 < len(rest):
            out.append(rest[i + 1])
            i += 2
            continue
        if ch == ",":
            break
        out.append(ch)
        i += 1
    return "".join(out).strip().strip('"')


def verify(path, expected_sha=None, publisher=EXPECTED_PUBLISHER):
    """Prove the file is ours, or raise VerificationError. Returns a plain
    description of what actually passed, for the UI to show."""
    checks = []

    if expected_sha:
        actual = sha256_file(path)
        if actual.lower() != expected_sha.lower():
            raise VerificationError(
                "The download doesn't match the checksum the release "
                f"published (got {actual[:12]}…, expected {expected_sha[:12]}…).")
        checks.append("SHA256 checksum")

    sig_status, subject = authenticode(path)
    if sig_status != "Valid":
        raise VerificationError(
            f"Windows says the download's signature is \"{sig_status}\".")
    cn = subject_cn(subject)
    if not cn or cn.casefold() != publisher.casefold():
        raise VerificationError(
            "The download is signed by someone else "
            f"({subject or 'unknown signer'}), not {publisher}.")
    checks.append(f"Authenticode signature ({publisher})")

    if not expected_sha:
        return (" and ".join(checks)
                + " (this release published no checksum file)")
    return " and ".join(checks)


# ---------------- download ----------------

def _download(url, dest, on_progress=None):
    tmp = dest + ".part"
    with requests.get(url, headers={"User-Agent": _HEADERS["User-Agent"],
                                    "Accept": "application/octet-stream"},
                      timeout=_TIMEOUT, stream=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done, total)
    if os.path.exists(dest):
        os.remove(dest)
    os.replace(tmp, dest)
    return dest


def _run_pipeline():
    ok, reason = supported()
    if not ok:
        _fail(reason)
        return

    _set(state="checking", message="Looking up the latest release…", progress=0)
    try:
        release = _fetch_release()
    except Exception as e:
        _fail(f"Couldn't reach GitHub ({e.__class__.__name__}).")
        return

    page = release.get("html_url") or DOWNLOAD_URL
    _set(download_url=page)

    asset = pick_installer(release)
    if not asset:
        _fail("This release doesn't include the Windows installer, so there's "
              "nothing to install automatically. Grab it from the releases "
              "page instead.")
        return

    name = asset["name"]
    expected_sha = None
    sums_asset = pick_checksums(release)
    if sums_asset:
        try:
            r = requests.get(sums_asset["browser_download_url"],
                             headers={"User-Agent": _HEADERS["User-Agent"]},
                             timeout=_TIMEOUT)
            r.raise_for_status()
            expected_sha = parse_checksums(r.text).get(name)
        except Exception:
            expected_sha = None  # signature check below still has to pass

    cache = paths.ensure(paths.cache_dir())
    dest = os.path.join(cache, name)

    _set(state="downloading", asset=name, progress=0,
         message=f"Downloading {name}…")

    def progress(done, total):
        pct = int(done * 100 / total) if total else 0
        _set(progress=pct,
             message=f"Downloading {name}… {pct}%" if total
                     else f"Downloading {name}… {done // 1024} KB")

    try:
        _download(asset["browser_download_url"], dest, progress)
    except Exception as e:
        _fail(f"Download failed ({e.__class__.__name__}).")
        return

    _set(state="verifying", progress=100,
         message="Checking the download's checksum and signature…")
    try:
        verified = verify(dest, expected_sha)
    except Exception as e:
        # Only this branch may say the file is gone, because only this branch
        # deletes it. verify() itself reports the reason and nothing more.
        reason = (str(e) if isinstance(e, VerificationError)
                  else f"Verification failed ({e.__class__.__name__}: {e}).")
        gone = True
        try:
            os.remove(dest)
        except OSError:
            gone = False
        _fail(reason + (" It was not run and has been deleted."
                        if gone else " It was not run."))
        return

    _set(state="ready", path=dest, verified=verified, expected_sha=expected_sha,
         message=f"Verified {name} ({verified}). Ready to install.")


def start():
    """Kick off download and verification. Never runs anything."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return dict(_state)
    _reset()
    _thread = threading.Thread(target=_run_pipeline, daemon=True)
    _thread.start()
    return status()


# ---------------- install ----------------

def install_command(path):
    """Inno Setup flags: /SILENT still shows a progress window (the user can
    see what is happening) but asks nothing, /CLOSEAPPLICATIONS lets it deal
    with our exe if it's somehow still up, and the installer's silent-mode
    [Run] entry relaunches the app when it's done."""
    return [path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"]


def launch():
    """Run the verified installer. Refuses unless verification actually
    passed -- this is the last gate between a download and code execution."""
    snap = status()
    if snap["state"] != "ready" or not snap["verified"] or not snap["path"]:
        raise VerificationError(
            "No verified installer to run. Download and verify an update first.")
    path = snap["path"]
    if not os.path.exists(path):
        _fail("The verified installer is gone from the cache. Try again.")
        raise VerificationError("The verified installer is no longer on disk.")

    # Re-verify the exact file we're about to execute, right now. The download
    # verified earlier, but it has been sitting in a user-writable cache since;
    # anything that swapped or edited it in that window has to be caught here,
    # or the earlier check guarded a different set of bytes than the ones we
    # run. Same path, same expected hash, same signature rules.
    try:
        verify(path, snap.get("expected_sha"))
    except Exception as e:
        reason = (str(e) if isinstance(e, VerificationError)
                  else f"Re-check before launch failed ({e.__class__.__name__}: {e}).")
        gone = True
        try:
            os.remove(path)
        except OSError:
            gone = False
        _fail("The installer changed after it was verified, so it was not run"
              + (" and has been deleted. " if gone else ". ") + reason)
        raise VerificationError(reason)

    flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    subprocess.Popen(install_command(path), close_fds=True, creationflags=flags)
    _set(state="installing", progress=100,
         message="Installer launched. The app is closing so it can be "
                 "replaced, and it will reopen when the install finishes.")
    return status()
