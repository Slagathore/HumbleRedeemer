"""The gate between "there's a new build" and "we ran an exe".

update_install downloads a release asset and executes it. That is the most
dangerous thing this app does, so the tests here are mostly about refusal:
a wrong checksum, an unsigned file, a file signed by somebody else, or a
download that never got verified at all must all end with the installer NOT
launched. Every test that could let one through asserts on a fake Popen that
records whether anything was executed.

Nothing here touches the network: the release JSON, the download and the
Windows signature check are all seams the tests replace.
"""
import os

import pytest

from redeemer import update_install as ui

GOOD_SUBJECT = "CN=Charles Chambers, O=Charles Chambers, C=US"


class FakePopen:
    """Stands in for subprocess.Popen and remembers if it was ever called."""

    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        return self

    @property
    def launched(self):
        return bool(self.calls)


@pytest.fixture
def popen(monkeypatch):
    fake = FakePopen()
    monkeypatch.setattr(ui.subprocess, "Popen", fake)
    return fake


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Frozen Windows build, with its data and update cache under tmp_path."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.delenv("HUMBLEREDEEMER_DATA", raising=False)
    monkeypatch.setattr(ui.paths, "frozen", lambda: True)
    monkeypatch.setattr(ui.os, "name", "nt")
    monkeypatch.setattr(ui.sys, "platform", "win32")
    ui._reset()
    yield
    ui._reset()


def _installer(tmp_path, body=b"pretend installer"):
    path = tmp_path / "HumbleRedeemer-1.2.3-Setup.exe"
    path.write_bytes(body)
    return str(path)


def _release(assets):
    return {"html_url": "https://example.invalid/releases/v1.2.3",
            "assets": [{"name": n, "browser_download_url": u}
                       for n, u in assets]}


def _signature(status, subject=GOOD_SUBJECT):
    return lambda path: (status, subject)


# ---------------- picking what to download ----------------

def test_installer_asset_is_preferred_over_the_bare_exe_and_zip():
    release = _release([("HumbleRedeemer.exe", "u1"),
                        ("HumbleRedeemer-windows.zip", "u2"),
                        ("HumbleRedeemer-1.2.3-Setup.exe", "u3"),
                        ("SHA256SUMS.txt", "u4")])
    assert ui.pick_installer(release)["name"] == "HumbleRedeemer-1.2.3-Setup.exe"
    assert ui.pick_checksums(release)["name"] == "SHA256SUMS.txt"


def test_no_installer_asset_means_no_download_target():
    release = _release([("HumbleRedeemer.exe", "u1"),
                        ("HumbleRedeemer-windows.zip", "u2")])
    assert ui.pick_installer(release) is None
    assert ui.pick_checksums(release) is None


def test_parse_checksums_ignores_junk():
    text = ("# a comment\n"
            "deadbeef  short-digest-not-64-hex.exe\n"
            f"{'a' * 64}  HumbleRedeemer-1.2.3-Setup.exe\n"
            f"{'b' * 64} *HumbleRedeemer.exe\n")
    sums = ui.parse_checksums(text)
    assert sums == {"HumbleRedeemer-1.2.3-Setup.exe": "a" * 64,
                    "HumbleRedeemer.exe": "b" * 64}
    assert "short-digest-not-64-hex.exe" not in sums


# ---------------- verification refuses ----------------

def test_verify_accepts_a_matching_hash_and_our_signature(tmp_path, monkeypatch):
    path = _installer(tmp_path)
    monkeypatch.setattr(ui, "authenticode", _signature("Valid"))
    verified = ui.verify(path, ui.sha256_file(path))
    assert "SHA256" in verified
    assert ui.EXPECTED_PUBLISHER in verified


def test_verify_refuses_a_bad_hash(tmp_path, monkeypatch):
    path = _installer(tmp_path)
    monkeypatch.setattr(ui, "authenticode", _signature("Valid"))
    with pytest.raises(ui.VerificationError, match="checksum"):
        ui.verify(path, "f" * 64)


def test_verify_refuses_an_unsigned_download(tmp_path, monkeypatch):
    path = _installer(tmp_path)
    monkeypatch.setattr(ui, "authenticode", _signature("NotSigned", ""))
    with pytest.raises(ui.VerificationError, match="NotSigned"):
        ui.verify(path, ui.sha256_file(path))


def test_verify_refuses_a_tampered_signature(tmp_path, monkeypatch):
    path = _installer(tmp_path)
    monkeypatch.setattr(ui, "authenticode", _signature("HashMismatch"))
    with pytest.raises(ui.VerificationError):
        ui.verify(path, ui.sha256_file(path))


def test_verify_refuses_someone_elses_certificate(tmp_path, monkeypatch):
    path = _installer(tmp_path)
    monkeypatch.setattr(ui, "authenticode",
                        _signature("Valid", "CN=Definitely Not Cole, C=XX"))
    with pytest.raises(ui.VerificationError, match="signed by someone else"):
        ui.verify(path, ui.sha256_file(path))


def test_verify_still_demands_a_signature_when_no_checksum_is_published(
        tmp_path, monkeypatch):
    path = _installer(tmp_path)
    monkeypatch.setattr(ui, "authenticode", _signature("NotSigned", ""))
    with pytest.raises(ui.VerificationError):
        ui.verify(path, None)

    monkeypatch.setattr(ui, "authenticode", _signature("Valid"))
    verified = ui.verify(path, None)
    assert ui.EXPECTED_PUBLISHER in verified
    assert "no checksum" in verified


# ---------------- nothing runs unless it verified ----------------

def test_launch_refuses_when_nothing_has_been_verified(popen):
    with pytest.raises(ui.VerificationError):
        ui.launch()
    assert not popen.launched


def test_launch_refuses_a_failed_verification(tmp_path, popen):
    ui._set(state="failed", path=_installer(tmp_path), verified="",
            error="bad signature")
    with pytest.raises(ui.VerificationError):
        ui.launch()
    assert not popen.launched


def test_launch_refuses_a_download_that_only_claims_to_be_ready(tmp_path, popen):
    # state says ready but verification never actually recorded a pass
    ui._set(state="ready", path=_installer(tmp_path), verified="")
    with pytest.raises(ui.VerificationError):
        ui.launch()
    assert not popen.launched


def test_launch_runs_the_verified_installer_silently(tmp_path, popen):
    path = _installer(tmp_path)
    ui._set(state="ready", path=path, verified="SHA256 and Authenticode")
    state = ui.launch()
    assert popen.calls == [ui.install_command(path)]
    assert popen.calls[0][0] == path
    assert "/SILENT" in popen.calls[0]
    assert state["state"] == "installing"
    # never claims the install finished -- the app is about to die
    assert "installed" not in state["message"].lower()


# ---------------- the whole pipeline ----------------

def _wire_pipeline(monkeypatch, tmp_path, release, sig_status="Valid",
                   sums_text=None, body=b"pretend installer"):
    monkeypatch.setattr(ui, "_fetch_release", lambda: release)
    monkeypatch.setattr(ui, "authenticode", _signature(sig_status))

    def fake_download(url, dest, on_progress=None):
        with open(dest, "wb") as f:
            f.write(body)
        if on_progress:
            on_progress(len(body), len(body))
        return dest

    monkeypatch.setattr(ui, "_download", fake_download)

    class FakeResponse:
        text = sums_text or ""

        def raise_for_status(self):
            pass

    monkeypatch.setattr(ui.requests, "get", lambda *a, **k: FakeResponse())


def _run(monkeypatch):
    ui.start()
    ui._thread.join(timeout=10)
    return ui.status()


def test_pipeline_verifies_then_stops_at_ready_without_running_anything(
        tmp_path, monkeypatch, popen):
    import hashlib
    body = b"pretend installer"
    digest = hashlib.sha256(body).hexdigest()
    release = _release([("HumbleRedeemer-1.2.3-Setup.exe", "https://x/setup"),
                        ("SHA256SUMS.txt", "https://x/sums")])
    _wire_pipeline(monkeypatch, tmp_path, release, body=body,
                   sums_text=f"{digest}  HumbleRedeemer-1.2.3-Setup.exe\n")

    state = _run(monkeypatch)

    assert state["state"] == "ready"
    assert "SHA256" in state["verified"]
    assert os.path.exists(state["path"])
    assert not popen.launched  # verifying is not installing


def test_pipeline_refuses_a_tampered_download_and_deletes_it(
        tmp_path, monkeypatch, popen):
    release = _release([("HumbleRedeemer-1.2.3-Setup.exe", "https://x/setup"),
                        ("SHA256SUMS.txt", "https://x/sums")])
    _wire_pipeline(monkeypatch, tmp_path, release,
                   sums_text=f"{'0' * 64}  HumbleRedeemer-1.2.3-Setup.exe\n")

    state = _run(monkeypatch)

    assert state["state"] == "failed"
    assert "checksum" in state["error"]
    assert not state["verified"] and not state["path"]
    assert not popen.launched
    cached = os.path.join(ui.paths.cache_dir(), "HumbleRedeemer-1.2.3-Setup.exe")
    assert not os.path.exists(cached)  # the bad file doesn't sit around

    with pytest.raises(ui.VerificationError):
        ui.launch()
    assert not popen.launched


def test_pipeline_refuses_an_unsigned_download(tmp_path, monkeypatch, popen):
    release = _release([("HumbleRedeemer-1.2.3-Setup.exe", "https://x/setup")])
    _wire_pipeline(monkeypatch, tmp_path, release, sig_status="NotSigned")

    state = _run(monkeypatch)

    assert state["state"] == "failed"
    assert "NotSigned" in state["error"]
    assert not popen.launched


def test_pipeline_says_so_when_the_release_ships_no_installer(
        tmp_path, monkeypatch, popen):
    release = _release([("HumbleRedeemer.exe", "https://x/exe"),
                        ("HumbleRedeemer-windows.zip", "https://x/zip")])
    _wire_pipeline(monkeypatch, tmp_path, release)

    state = _run(monkeypatch)

    assert state["state"] == "failed"
    assert "installer" in state["error"]
    assert not popen.launched


# ---------------- source runs never get a binary ----------------

def test_source_run_is_told_to_git_pull(monkeypatch, popen):
    monkeypatch.setattr(ui.paths, "frozen", lambda: False)
    ok, reason = ui.supported()
    assert not ok
    assert "git pull" in reason

    state = _run(monkeypatch)  # even if something calls start() anyway
    assert state["state"] == "failed"
    assert "git pull" in state["error"]
    assert not popen.launched


def test_frozen_windows_build_can_install():
    ok, reason = ui.supported()
    assert ok and reason == ""


def test_authenticode_never_passes_off_windows(tmp_path, monkeypatch):
    """"Couldn't check" must never read as "checked out fine"."""
    monkeypatch.setattr(ui.os, "name", "posix")
    with pytest.raises(ui.VerificationError):
        ui.authenticode(_installer(tmp_path))
