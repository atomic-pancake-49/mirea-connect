"""Parent-owned storage acceptance; only synthetic persistent fixture data."""
import importlib
import importlib.util

import pytest
from pymirea import _settings
from pymirea.store import NotLoggedIn, SessionStore


@pytest.fixture
def retained_type(monkeypatch):
    previous = _settings._cfg
    _settings._cfg = None
    monkeypatch.setenv("PYMIREA_SESSION_KEYS", "synthetic-parent-test-key")
    try:
        spec = importlib.util.find_spec("mirea_connect.session_state")
        assert spec is not None, "shared retained local store is not implemented"
        yield importlib.import_module("mirea_connect.session_state").LocalSessionStore
    finally:
        _settings._cfg = previous


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


def test_candidate_roundtrip_is_encrypted_and_not_an_active_login(tmp_path, retained_type):
    root = tmp_path / "session"
    store = retained_type(root)
    store.configure()
    ident = store.save_candidate({".AspNetCore.Cookies": "SYNTHETIC-SECRET-COOKIE"}, login="SYNTHETIC-PRIVATE-LOGIN")
    assert isinstance(ident, str) and ident and "/" not in ident
    before = files(root)
    assert before
    assert all(b"SYNTHETIC-SECRET-COOKIE" not in value for value in before.values())
    assert all(b"SYNTHETIC-PRIVATE-LOGIN" not in value for value in before.values())
    with pytest.raises(NotLoggedIn):
        store.load()
    restored = retained_type(root)
    restored.configure()
    candidate = restored.load_candidate(ident)
    assert candidate.cookies[".AspNetCore.Cookies"] == "SYNTHETIC-SECRET-COOKIE"
    assert candidate.login == "SYNTHETIC-PRIVATE-LOGIN"
    assert candidate.candidate_id == ident
    assert files(root) == before
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            assert p.stat().st_mode & 0o077 == 0


def test_verification_and_refresh_append_without_replacing_legacy_or_candidates(tmp_path, retained_type):
    root = tmp_path / "session"
    legacy = SessionStore(root)
    legacy.configure()
    legacy.save({".AspNetCore.Cookies": "OLD-SYNTHETIC"}, login="legacy@example.invalid")
    old = files(root)
    store = retained_type(root)
    store.configure()
    ident = store.save_candidate({".AspNetCore.Cookies": "NEW-SYNTHETIC"}, login="new@example.invalid")
    pending_files = files(root)
    assert store.load().cookies[".AspNetCore.Cookies"] == "OLD-SYNTHETIC"
    store.mark_verified(ident)
    assert store.load().cookies[".AspNetCore.Cookies"] == "NEW-SYNTHETIC"
    verified_files = files(root)
    assert all(verified_files[k] == v for k, v in pending_files.items())
    store.save({".AspNetCore.Cookies": "NEW-SYNTHETIC", "__sdo_moodle_session": "MOODLE-SYNTHETIC"})
    final = files(root)
    assert all(final[k] == v for k, v in verified_files.items())
    assert all(final[k] == v for k, v in old.items())
    assert retained_type(root).load().cookies["__sdo_moodle_session"] == "MOODLE-SYNTHETIC"
    try:
        outcome = store.clear()
        assert outcome is False, "clear must refuse rather than claim deletion"
    except RuntimeError:
        pass
    assert files(root) == final


@pytest.mark.parametrize("selector", ["../session.json", "/absolute/path", "../../key", "id/other", "..", ""])
def test_candidate_selector_cannot_be_a_path(tmp_path, retained_type, selector):
    store = retained_type(tmp_path / "session")
    store.configure()
    store.save_candidate({"K": "SYNTHETIC"})
    with pytest.raises((ValueError, NotLoggedIn, OSError)):
        store.load_candidate(selector)


def test_malformed_new_record_cannot_hide_preserved_verified_session(tmp_path, retained_type):
    import json
    store = retained_type(tmp_path / "session")
    store.configure()
    store.save({"K": "SYNTHETIC-VERIFIED"})
    bad = store.records_path / ("record-99999999999999999999-" + "0" * 32 + ".json")
    with bad.open("x") as stream:
        json.dump({"schema": 1, "blob": 7}, stream)
    assert store.load().cookies == {"K": "SYNTHETIC-VERIFIED"}
    assert bad.exists()


def test_symlinked_store_root_is_rejected(tmp_path, retained_type):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises((ValueError, RuntimeError, OSError)):
        store = retained_type(link)
        store.configure()
        store.save_candidate({"K": "SYNTHETIC"})
