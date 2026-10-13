"""
Tests for how a finished slskd download gets into MusicGrabber's staging dir.

Copy has always been the behaviour and stays the default; move is opt-in via
`slskd_move_completed`. The rule that matters is simple: never delete the
source until the destination is provably the same file. Everything below is
some variation on "and what if it isn't?".
"""

import errno
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slskd


@pytest.fixture
def move_mode(monkeypatch):
    """Flip slskd_move_completed without going near the database."""
    def _set(enabled):
        monkeypatch.setattr(slskd, "get_setting_bool", lambda *_a, **_k: enabled)
    return _set


@pytest.fixture
def a_download(tmp_path):
    """A completed download sitting in slskd's folder, plus somewhere to put it."""
    root = tmp_path / "downloads"
    source = root / "generous_stranger" / "Paramore" / "Brand New Eyes" / "02 - Ignorance.flac"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fLaC" + b"pretend this is 45MB of Paramore" * 100)

    staging = tmp_path / "staging"
    staging.mkdir()

    return source, staging / source.name, [root]


def test_copy_is_still_the_default(move_mode, a_download):
    """Nobody's sharing folder gets quietly emptied because they upgraded."""
    move_mode(False)
    source, dest, roots = a_download
    original = source.read_bytes()

    slskd.deliver_slskd_file(source, dest, roots)

    assert source.exists(), "copy mode deleted the source"
    assert dest.read_bytes() == original


def test_move_on_one_filesystem_is_a_rename(move_mode, a_download, monkeypatch):
    """Same filesystem: instant, atomic, and no hashing of a 45MB file for nothing."""
    move_mode(True)
    source, dest, roots = a_download
    original = source.read_bytes()

    hashed = []
    monkeypatch.setattr(slskd, "_sha256_of", lambda p: hashed.append(p) or "nope")

    slskd.deliver_slskd_file(source, dest, roots)

    assert not source.exists()
    assert dest.read_bytes() == original
    assert not hashed, "a plain rename copies nothing, so there is nothing to verify"


def _pretend_different_filesystems(monkeypatch):
    """Make os.replace behave as it does across mounts, because tmp_path is one disk."""
    def _refuse(_src, _dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    monkeypatch.setattr(slskd.os, "replace", _refuse)


def test_move_across_filesystems_copies_verifies_then_deletes(move_mode, a_download, monkeypatch):
    move_mode(True)
    _pretend_different_filesystems(monkeypatch)
    source, dest, roots = a_download
    original = source.read_bytes()

    slskd.deliver_slskd_file(source, dest, roots)

    assert dest.read_bytes() == original
    assert not source.exists(), "verified copy should have taken the source with it"


def test_a_corrupt_copy_never_costs_you_the_source(move_mode, a_download, monkeypatch):
    """The whole reason for hashing. Bad copy in, source untouched, retry still possible."""
    move_mode(True)
    _pretend_different_filesystems(monkeypatch)
    source, dest, roots = a_download
    original = source.read_bytes()

    def _copy_badly(src, dst, **_kw):
        # Same length, different bytes: size alone would wave this straight through.
        with open(dst, "wb") as handle:
            handle.write(b"\x00" * len(original))
    monkeypatch.setattr(slskd.shutil, "copy2", _copy_badly)

    with pytest.raises(Exception, match="verification"):
        slskd.deliver_slskd_file(source, dest, roots)

    assert source.read_bytes() == original, "source was deleted despite a failed copy"
    assert not dest.exists(), "the dud copy was left lying about"


def test_a_truncated_copy_is_caught_on_size(move_mode, a_download, monkeypatch):
    move_mode(True)
    _pretend_different_filesystems(monkeypatch)
    source, dest, roots = a_download

    def _copy_short(_src, dst, **_kw):
        with open(dst, "wb") as handle:
            handle.write(b"fLaC")
    monkeypatch.setattr(slskd.shutil, "copy2", _copy_short)

    with pytest.raises(Exception, match="size mismatch"):
        slskd.deliver_slskd_file(source, dest, roots)

    assert source.exists()


def test_an_undeletable_source_does_not_fail_the_download(move_mode, a_download, monkeypatch, capsys):
    """Read-only mount, awkward permissions: the file arrived, so get on with it."""
    move_mode(True)
    _pretend_different_filesystems(monkeypatch)
    source, dest, roots = a_download
    original = source.read_bytes()

    real_unlink = os.unlink

    def _refuse_unlink(path, **kwargs):
        if str(path) == str(source):
            raise OSError(errno.EACCES, "Permission denied")
        return real_unlink(path, **kwargs)
    monkeypatch.setattr(slskd.os, "unlink", _refuse_unlink)

    result = slskd.deliver_slskd_file(source, dest, roots)

    assert result.read_bytes() == original
    assert "could not remove the source" in capsys.readouterr().out


def test_empty_folders_are_swept_up_but_the_root_survives(move_mode, a_download):
    move_mode(True)
    source, dest, roots = a_download
    album_dir = source.parent
    artist_dir = album_dir.parent
    user_dir = artist_dir.parent

    slskd.deliver_slskd_file(source, dest, roots)

    assert not album_dir.exists()
    assert not artist_dir.exists()
    assert not user_dir.exists()
    assert roots[0].exists(), "the downloads root itself must never be removed"


def test_pruning_stops_the_moment_something_else_is_there(move_mode, a_download):
    """Sweeping up is fine; taking somebody's other album with it is not."""
    move_mode(True)
    source, dest, roots = a_download
    sibling = source.parent.parent / "Riot!" / "01 - For a Pessimist.flac"
    sibling.parent.mkdir(parents=True)
    sibling.write_bytes(b"fLaC")

    slskd.deliver_slskd_file(source, dest, roots)

    assert not source.parent.exists(), "the emptied album folder should have gone"
    assert sibling.exists(), "pruning walked into a folder that was not empty"
    assert sibling.parent.parent.exists()


def test_pruning_refuses_to_wander_outside_the_download_roots(tmp_path):
    """Belt and braces: hand it a folder nowhere near slskd and it should decline."""
    outsider = tmp_path / "somebody-elses-empty-folder"
    outsider.mkdir()

    slskd.prune_empty_dirs(outsider, [tmp_path / "downloads"])

    assert outsider.exists()


def test_verify_copy_agrees_when_the_copy_is_honest(a_download):
    source, dest, _roots = a_download
    shutil.copy2(source, dest)

    verified, reason = slskd.verify_copy(source, dest)

    assert verified, reason
