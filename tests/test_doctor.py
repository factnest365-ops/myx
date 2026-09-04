from __future__ import annotations

import subprocess

import myx.cli as cli


def test_cli_doctor_parse_args():
    args = cli.parse_args(["doctor"])
    assert args.subcommand == "doctor"
    assert args.fix is False
    args2 = cli.parse_args(["doctor", "--fix"])
    assert args2.subcommand == "doctor"
    assert args2.fix is True


def test_doctor_reports_missing_bins(monkeypatch, tmp_path, capsys):
    # Simulate all bins missing
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "_bin_version", lambda name: None)
    # avoid lock/socket side effects - replace paths with tmp stubs
    monkeypatch.setattr(cli, "CMD_SOCK", tmp_path / "myx-cmd.sock")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / "myx.lock")

    report = cli.doctor_check()
    assert report["mpv"]["installed"] is False
    assert report["yt-dlp"]["installed"] is False
    assert report["ffmpeg"]["installed"] is False
    assert report["yt_dlp_stale"] is False

    # printing should mention MISSING
    cli._print_doctor(report, fix=False)
    out = capsys.readouterr().out
    assert "MISSING" in out
    assert "mpv" in out
    assert "yt-dlp" in out


def test_doctor_fix_suggests_brew(monkeypatch, tmp_path, capsys):
    # Missing bins, brew available, fix=False should suggest brew install
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None)
    monkeypatch.setattr(cli, "_bin_version", lambda name: None)
    monkeypatch.setattr(cli, "CMD_SOCK", tmp_path / "myx-cmd.sock")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / "myx.lock")

    report = cli.doctor_check()
    assert report["brew_available"] is True
    cli._print_doctor(report, fix=False)
    out = capsys.readouterr().out
    assert "brew install" in out

    # fix=True should attempt to run brew install (mock subprocess.run)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli._print_doctor(report, fix=True)
    capsys.readouterr()
    # should have called brew install with missing bins
    assert any("brew" in c and "install" in c for c in calls)


def test_friendly_error_instead_of_raw_hint(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    try:
        cli.check_bins()
        assert False, "should have raised SystemExit"
    except SystemExit as exc:
        msg = str(exc)
        assert "myx doctor" in msg
        assert "missing required binary" in msg


def test_doctor_stale_detection(monkeypatch, tmp_path):
    # yt-dlp installed with old version -> stale
    def fake_which(name):
        return f"/usr/bin/{name}"

    monkeypatch.setattr(cli.shutil, "which", fake_which)

    def fake_version(name):
        if name == "yt-dlp":
            return "2023.01.01"
        return "1.0.0"

    monkeypatch.setattr(cli, "_bin_version", fake_version)
    monkeypatch.setattr(cli, "CMD_SOCK", tmp_path / "myx-cmd.sock")
    monkeypatch.setattr(cli, "LOCK_PATH", tmp_path / "myx.lock")
    report = cli.doctor_check()
    assert report["yt_dlp_stale"] is True

    # recent version not stale
    monkeypatch.setattr(cli, "_bin_version", lambda name: "2099.12.31" if name == "yt-dlp" else "1.0.0")
    report2 = cli.doctor_check()
    assert report2["yt_dlp_stale"] is False


def test_doctor_cleans_stale_lock(monkeypatch, tmp_path, capsys):
    # Simulate stale lock file
    fake_lock = tmp_path / "myx.lock"
    fake_lock.write_text("999999")
    monkeypatch.setattr(cli, "LOCK_PATH", fake_lock)
    monkeypatch.setattr(cli, "CMD_SOCK", tmp_path / "myx-cmd.sock")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/fake")
    monkeypatch.setattr(cli, "_bin_version", lambda name: "2099.12.31" if name == "yt-dlp" else "1.0.0")
    # pid 999999 not running
    monkeypatch.setattr(cli, "_pid_running", lambda pid: False)

    report = cli.doctor_check()
    assert report["daemon"]["stale_lock"] is True

    # --fix should clean up
    cli._print_doctor(report, fix=True)
    assert not fake_lock.exists()
