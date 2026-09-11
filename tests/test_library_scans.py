"""Slow library scans stay in the background and publish only complete results."""

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import library_scans
import utils


def wait_until(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline, 'background worker did not finish'
        time.sleep(.005)


def test_listing_cold_scan_returns_immediately_and_shares_worker():
    cache = library_scans.LibrarySnapshots()
    release = threading.Event()
    calls = []

    def scan():
        calls.append(1)
        assert release.wait(2)
        return ['Example']

    try:
        start = time.monotonic()
        for _ in range(10):
            assert cache.get(('alice', '/music'), scan) == (None, 'refreshing')
        assert time.monotonic() - start < .5
        wait_until(lambda: len(calls) == 1)
    finally:
        release.set()
    wait_until(lambda: not cache.entries[('alice', '/music')]['running'])
    assert cache.get(('alice', '/music'), scan) == (['Example'], 'ready')
    assert len(calls) == 1
    assert cache.get(('bob', '/other'), lambda: ['Other'])[0] is None
    wait_until(lambda: not cache.entries[('bob', '/other')]['running'])


def test_failed_refresh_preserves_complete_snapshot(monkeypatch):
    cache = library_scans.LibrarySnapshots()
    cache.entries['library'] = dict(value=['Previous'], finished=1, running=False, error=False)

    def fail():
        raise OSError('NAS unavailable')

    assert cache.get('library', fail) == (['Previous'], 'refreshing')
    wait_until(lambda: not cache.entries['library']['running'])
    assert cache.get('library', fail) == (['Previous'], 'error')


def test_storage_deduplicates_hardlinks_and_excludes_trash(tmp_path):
    song = tmp_path / 'song.flac'
    song.write_bytes(b'12345')
    (tmp_path / 'copy.flac').hardlink_to(song)
    trash = tmp_path / '@Recycle'
    trash.mkdir()
    (trash / 'deleted.flac').write_bytes(b'12345')
    assert library_scans.scan_storage((tmp_path,)) == {'storage_bytes': 5, 'file_count': 1}


@pytest.fixture
def tag_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, '_tag_index_cache', {})
    monkeypatch.setattr(utils, '_tag_index_running', {})
    monkeypatch.setattr(utils, '_tag_index_errors', {})
    monkeypatch.setattr(utils, 'get_setting', lambda *a, **kw: str(tmp_path))
    return tmp_path


def test_duplicate_scan_never_blocks_and_worker_is_shared(tag_cache, monkeypatch):
    release = threading.Event()
    calls = []
    song = tag_cache / 'renamed.flac'
    song.write_bytes(b'123')

    def scan(**kwargs):
        calls.append(1)
        assert release.wait(2)
        return {utils._tag_identity('Blur', 'Song 2'): song}

    monkeypatch.setattr(utils, 'build_library_tag_index', scan)
    try:
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(utils._tagged_duplicate, 'Blur', 'Song 2', 'alice') for _ in range(4)]
            for future in futures:
                assert future.result(timeout=1) is None
        assert time.monotonic() - started < .5
        assert len(calls) == 1
        finished_after = time.monotonic()
    finally:
        release.set()
    wait_until(lambda: not utils._tag_index_running)
    snapshot = next(iter(utils._tag_index_cache.values()))
    assert snapshot[0] >= finished_after
    assert utils._tagged_duplicate('Blur', 'Song 2', 'alice') == song
    assert utils._tagged_duplicate('Missing', 'Track', 'alice') is None
    assert len(calls) == 1


def test_failed_tag_scan_does_not_publish_empty_index(tag_cache, monkeypatch):
    def fail(**kwargs):
        raise OSError('NAS unavailable')
    monkeypatch.setattr(utils, 'build_library_tag_index', fail)
    assert utils._tagged_duplicate('Blur', 'Song 2', 'alice') is None
    wait_until(lambda: not utils._tag_index_running)
    assert not utils._tag_index_cache
    assert utils._tagged_duplicate('Blur', 'Song 2', 'alice') is None


def load_route(name, namespace):
    """Exercise the real handler without starting app.py's production schedulers."""
    import ast
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'app.py').read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    function.decorator_list = []
    namespace['Request'] = object
    exec(compile(ast.Module(body=[function], type_ignores=[]), 'app.py', 'exec'), namespace)
    return namespace[name]


def test_stats_releases_database_and_returns_while_storage_is_blocked(tmp_path):
    import sqlite3
    from contextlib import contextmanager
    from types import SimpleNamespace

    conn = sqlite3.connect(':memory:')
    conn.executescript('''
        CREATE TABLE jobs (status TEXT, source TEXT, completed_at TEXT, artist TEXT, title TEXT, search_token TEXT);
        CREATE TABLE search_logs (result_count INTEGER, artist TEXT, search_token TEXT);
        INSERT INTO jobs VALUES ('completed', 'youtube', datetime('now'), 'Blur', 'Song 2', 's1');
    ''')
    held = []
    release = threading.Event()
    started = threading.Event()
    cache = library_scans.LibrarySnapshots()

    @contextmanager
    def connection():
        held.append(True)
        try:
            yield conn
        finally:
            held.pop()

    def storage(directories):
        assert not held, 'filesystem scan still holds the database connection'
        started.set()
        assert release.wait(2)
        return {'storage_bytes': 123, 'file_count': 1}

    route = load_route('get_stats', {
        'db_conn': connection, 'sqlite3': sqlite3, 'snapshots': cache,
        'get_singles_dir': lambda: tmp_path, 'get_playlists_dir': lambda: None,
        'scan_storage': storage,
    })
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))
    try:
        result = route(request)
        assert started.wait(1)
        assert result['completed'] == 1
        assert result['file_count'] is None
        assert result['storage_scan_state'] == 'refreshing'
    finally:
        release.set()
    wait_until(lambda: all(not entry['running'] for entry in cache.entries.values()))
    assert route(request)['storage_bytes'] == 123
    conn.close()


def test_playlists_returns_scoped_watches_before_disk_scan(tmp_path):
    import sqlite3
    from contextlib import contextmanager
    from types import SimpleNamespace

    conn = sqlite3.connect(':memory:')
    conn.executescript('''
        CREATE TABLE watched_playlists (id TEXT, name TEXT, platform TEXT, sync_mode TEXT, user_id TEXT);
        INSERT INTO watched_playlists VALUES ('a', 'Alice list', 'spotify', 'append', 'alice');
        INSERT INTO watched_playlists VALUES ('b', 'Bob list', 'spotify', 'append', 'bob');
    ''')
    release = threading.Event()
    cache = library_scans.LibrarySnapshots()

    @contextmanager
    def connection():
        yield conn

    def scan(directory):
        assert release.wait(2)
        return ['Physical list']

    route = load_route('list_playlists', {
        'db_conn': connection, 'sqlite3': sqlite3, 'snapshots': cache,
        'get_playlists_dir': lambda **kw: tmp_path / kw['user_id'],
        'scan_playlists': scan, '_user_scope': lambda user, admin: ('user_id = ?', [user]),
    })
    try:
        for user, expected in [('alice', 'Alice list'), ('bob', 'Bob list')]:
            request = SimpleNamespace(state=SimpleNamespace(user_id=user, is_admin=False))
            result = route(request)
            assert [row['name'] for row in result['playlists']] == [expected]
            assert result['scan_ready'] is False
    finally:
        release.set()
    wait_until(lambda: all(not entry['running'] for entry in cache.entries.values()))
    assert len(cache.entries) == 2
    conn.close()


def test_strict_tag_scan_rejects_unavailable_root(tag_cache):
    tag_cache.rmdir()
    with pytest.raises(FileNotFoundError):
        utils.build_library_tag_index(user_id='alice')


def test_new_library_without_optional_folders_has_zero_storage(tmp_path):
    assert library_scans.scan_storage((tmp_path / 'Singles', tmp_path / 'Playlists')) == {
        'storage_bytes': 0, 'file_count': 0,
    }


def test_playlist_refresh_preserves_new_playlist_choice():
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to execute the browser function')
    source = (Path(__file__).resolve().parents[1] / 'static/app.js').read_text()
    function = source[source.index('        function _populatePlaylistSelector()'):source.index('        function getSelectedPlaylist()')]
    script = '''
        const assert = require('node:assert/strict');
        const sel = {
            value: '__new__', options: [{value: ''}, {value: '__new__'}],
            remove(index) { this.options.splice(index, 1); this.value = ''; },
            appendChild(option) { this.options.push(option); },
        };
        const document = {
            getElementById() { return sel; },
            createElement() { return {dataset: {}}; },
        };
        const _playlists = [{name: 'Newly scanned playlist', is_watched: false}];
    ''' + function + '''
        _populatePlaylistSelector();
        assert.equal(sel.value, '__new__');
        assert.ok(sel.options.some(option => option.value === 'Newly scanned playlist'));
    '''
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)


def test_late_unauthorised_response_preserves_new_session():
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to execute the browser function')
    source = (Path(__file__).resolve().parents[1] / 'static/app.js').read_text()
    start = source.index('        async function apiFetch(')
    function = source[start:source.index('        // =============================================================================', start)]
    script = '''
        const assert = require('node:assert/strict');
        let token = 'old';
        let loginShown = 0;
        let resolve;
        const getSessionToken = () => token;
        const setSessionToken = value => { token = value; };
        const setCurrentUser = () => {};
        const showLoginScreen = () => { loginShown++; };
        const withRootPath = url => url;
        const serverConfig = {users_exist: true};
        const fetch = () => new Promise(r => { resolve = r; });
    ''' + function + '''
        (async () => {
            const oldRequest = apiFetch('/api/playlists');
            token = 'new';
            resolve({status: 401});
            await assert.rejects(oldRequest, /Authentication required/);
            assert.equal(token, 'new');
            assert.equal(loginShown, 0);
            const expiredRequest = apiFetch('/api/playlists');
            resolve({status: 401});
            await assert.rejects(expiredRequest, /Authentication required/);
            assert.equal(token, null);
            assert.equal(loginShown, 1);
        })().catch(error => { console.error(error); process.exitCode = 1; });
    '''
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)
