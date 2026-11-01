"""Credential-free tests for the 403 cooldown (breaker) in yt_download.
Extracts only the functions under test from app.py; no app startup or network.
Race tests use real threads against the real locks; the gate test uses the real
_ytdlp_serial so the admission recheck really runs under the real download lock."""
import ast
import fnmatch
import io
import logging
import re
import sys
import threading
import time as real_time
import types
import unittest
from pathlib import Path

source = Path(__file__).resolve().parents[1].joinpath('app.py').read_text()
tree = ast.parse(source)
NAMES = ('_yt_is_403', '_yt_cb_record_403', '_yt_cb_route', '_yt_cb_would_route_tv', '_yt_cb_claim_probe',
         '_yt_cb_probe_done', '_yt_cb_record', '_yt_snap', '_yt_media_new', '_yt_attempt_cleanup',
         '_kill_deno', 'cancel_yt_except', '_ytdlp_serial', 'yt_download_tv', 'yt_download')
functions = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in NAMES:
        node.decorator_list = []
        functions.append(node)

class FakeTime:
    def __init__(self): self.now = 1000.0
    def time(self): return self.now
    def sleep(self, s): self.now += s

class FakeFs:
    """path -> [mtime_ns, size]; tests mutate it to simulate a download writing media."""
    def __init__(self):
        self.files = {}
        self.clock = 1
    def write(self, p, size=100):
        self.clock += 1
        self.files[p] = [self.clock, size]
    def remove(self, p):
        if p not in self.files:
            raise FileNotFoundError(p)
        del self.files[p]
    def stat(self, p):
        if p not in self.files:
            raise FileNotFoundError(p)
        m, s = self.files[p]
        return types.SimpleNamespace(st_mtime_ns=m, st_size=s)
    def isfile(self, p): return p in self.files
    def getsize(self, p): return self.files[p][1]
    def glob(self, pattern):
        return sorted(p for p in self.files if fnmatch.fnmatchcase(p, pattern))

class patch_modules:
    """app.py does `import glob` / `import yt_dlp` inside functions: patch sys.modules at call time."""
    def __init__(self, **mods): self.mods = mods
    def __enter__(self):
        self.prev = {k: sys.modules.get(k) for k in self.mods}
        sys.modules.update(self.mods)
    def __exit__(self, *a):
        for k, v in self.prev.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

def glob_mod(fs):
    m = types.ModuleType('glob')
    m.glob = fs.glob
    return m

def ytdlp_mods(ydl_cls):
    """Fake yt_dlp module chain, including the extractor submodule yt_download_tv imports."""
    base = types.ModuleType('yt_dlp.extractor.youtube._base')
    base.INNERTUBE_CLIENTS = {'tv': {'INNERTUBE_CONTEXT': {'client': {}}}}
    pkg_yt = types.ModuleType('yt_dlp.extractor.youtube')
    pkg_yt._base = base
    pkg_ex = types.ModuleType('yt_dlp.extractor')
    pkg_ex.youtube = pkg_yt
    top = types.ModuleType('yt_dlp')
    top.extractor = pkg_ex
    if ydl_cls is not None:
        top.YoutubeDL = ydl_cls
    return {'yt_dlp': top, 'yt_dlp.extractor': pkg_ex,
            'yt_dlp.extractor.youtube': pkg_yt, 'yt_dlp.extractor.youtube._base': base}

class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL; behavior hooks set per test."""
    on_enter = None
    on_download = None
    def __init__(self, opts): self.opts = opts
    def __enter__(self):
        if FakeYDL.on_enter: FakeYDL.on_enter()
        return self
    def __exit__(self, *a): return False
    def download(self, urls):
        if FakeYDL.on_download: FakeYDL.on_download(self.opts, urls)

def env_for(extra=None, fs=None, real_tv=False):
    fs = fs or FakeFs()
    ft = FakeTime()
    def fake_open(path, *a, **k):
        raise FileNotFoundError(path)
    fake_os = types.SimpleNamespace(
        path=types.SimpleNamespace(isdir=lambda p: True, join=lambda *a: '/'.join(a),
                                   isfile=fs.isfile, getsize=fs.getsize),
        listdir=lambda p: ['x'], environ={}, stat=fs.stat, remove=fs.remove)
    env = {'open': fake_open, 'time': ft, '__ft': ft, 'os': fake_os,
           'log': logging.getLogger('t'), 're': re, 'threading': threading,
           'YT_REFRESH_TOKEN': '', '_COOKIE_BOOT': {'configured': False},
           '_YT_SOLVER_READY': {'ok': True},
           '_YT_CB': {'fails': 0, 'open_until': 0.0, 'probe': None}, '_YT_CB_LOCK': threading.Lock(),
           '_YT_CB_THRESHOLD': 2, '_YT_CB_COOLDOWN_S': 1200.0,
           '_yt_dl_lock': threading.Lock(), '_yt_active': {},
           '_YtCooldownOpened': type('_YtCooldownOpened', (Exception,), {}),
           'SongTooLongError': type('SongTooLongError', (ValueError,), {}),
           'YtCancelled': type('YtCancelled', (Exception,), {}),
           'PS4_UA': 'ua', 'TV_CLIENT_VER': '1.0', '_YT': {'vd': 'x'},
           'yt_player': lambda vid: ('t', 60, 'OK'), '_yt_token': lambda: 'tok',
           'yt_download_tv': lambda *a, **k: (_ for _ in ()).throw(RuntimeError('tv-disabled-in-test')),
           '_song_duration_filter': lambda *a: None, '_active_cookie_file': lambda: None,
           '_yt_runtime_choices': lambda: ['qjs'], '_yt_runtime_opts': lambda rt: {},
           '_SolverLogger': lambda: types.SimpleNamespace(solver_failed=False),
           '_yt_cache_prune': lambda: None, '_yt_wait_headroom': lambda *a, **k: None,
           '_ytdlp_serial': None, 'yt_dlp': types.SimpleNamespace()}
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<cbtest>', 'exec'), env)
    # default stub shadowed by the extracted real function (real_tv keeps it; extra wins last)
    if not real_tv:
        env['yt_download_tv'] = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('tv-disabled-in-test'))
    if extra:
        env.update(extra)
    return env

def open_breaker(e, now=1000.0):
    """Drive the breaker into cooldown with two consecutive HTTP-403 primary failures."""
    e['__ft'].now = now
    e['_yt_cb_record_403']()
    e['_yt_cb_record_403']()

def serial_ok(fs, calls=None, path='/tmp/x.mp3'):
    """Fake primary download: runs the gate like the real one, then writes media."""
    def serial(yt_dlp, opts, video_id, gate=None, on_error=None):
        if gate is not None:
            gate()
        if calls is not None:
            calls.append('primary')
        fs.write(path)
        return {'title': 'x', 'duration': 60}
    return serial

def serial_fail(err, calls=None, cancelled=False):
    """Fake primary failure: runs gate and on_error like the real one (on_error is
    skipped for ev-set cancellations, mirroring _ytdlp_serial's except order)."""
    def serial(yt_dlp, opts, video_id, gate=None, on_error=None):
        if gate is not None:
            gate()
        if calls is not None:
            calls.append('primary')
        if on_error is not None and not cancelled:
            on_error(err)
        raise err
    return serial

class Matcher(unittest.TestCase):
    def test_tight_403_matching(self):
        e = env_for()
        self.assertTrue(e['_yt_is_403'](Exception('HTTP Error 403: Forbidden')))
        self.assertTrue(e['_yt_is_403'](Exception('HTTP Error 403 ')))
        self.assertFalse(e['_yt_is_403'](Exception('video abc403def unavailable')))  # id fragment
        self.assertFalse(e['_yt_is_403'](Exception('HTTP Error 429: Too Many Requests')))  # 429 not bypassed
        self.assertFalse(e['_yt_is_403'](Exception('403')))

class StreakAndRouting(unittest.TestCase):
    def test_non_403_breaks_consecutive_streak(self):
        e = env_for()
        e['_yt_cb_record_403']()
        e['_yt_cb_record'](ok=False, err=Exception('network timeout'))  # final non-403: streak broken
        e['_yt_cb_record_403']()
        self.assertEqual(e['_YT_CB']['fails'], 1)
        self.assertEqual(e['_yt_cb_route']()[0], 'primary')  # never reached threshold
    def test_success_resets_streak(self):
        e = env_for()
        e['_yt_cb_record_403']()
        e['_yt_cb_record'](ok=True)
        e['_yt_cb_record_403']()
        self.assertEqual(e['_YT_CB']['fails'], 1)
        self.assertEqual(e['_yt_cb_route']()[0], 'primary')
    def test_final_403_settlement_does_not_double_count(self):
        e = env_for()
        e['_yt_cb_record_403']()                                   # the moment it happened
        e['_yt_cb_record'](ok=False, err=Exception('HTTP Error 403: Forbidden'))  # chain settles
        self.assertEqual(e['_YT_CB']['fails'], 1)                  # counted once
    def test_would_route_tv_is_pure_read(self):
        e = env_for()
        open_breaker(e)
        e['__ft'].now += 1201  # expired
        self.assertTrue(e['_yt_cb_would_route_tv']())
        self.assertIsNone(e['_YT_CB']['probe'])  # the read never claims the probe slot

class OwnerToken(unittest.TestCase):
    def test_probe_release_only_by_holder(self):
        e = env_for()
        open_breaker(e)
        tok = e['_yt_cb_claim_probe']()
        self.assertIsNotNone(tok)
        e['_yt_cb_probe_done'](object())              # wrong token: no-op
        self.assertIs(e['_YT_CB']['probe'], tok)
        e['_yt_cb_record'](ok=True, token=object())   # stale token: outcome ignored entirely
        self.assertIs(e['_YT_CB']['probe'], tok)
        self.assertEqual(e['_YT_CB']['fails'], 2)
        e['_yt_cb_probe_done'](tok)                   # holder releases
        self.assertIsNone(e['_YT_CB']['probe'])
    def test_normal_caller_never_touches_probe_slot(self):
        e = env_for()
        open_breaker(e)
        tok = e['_yt_cb_claim_probe']()
        e['_yt_cb_record_403']()                             # a normal caller's 403
        self.assertEqual(e['_YT_CB']['fails'], 3)            # streak still updates (real data)
        self.assertIs(e['_YT_CB']['probe'], tok)             # but the slot is not freed
        e['_yt_cb_record'](ok=True)                          # normal success
        self.assertEqual(e['_YT_CB']['fails'], 0)
        self.assertIs(e['_YT_CB']['probe'], tok)             # slot still owned by the probe
        e['_yt_cb_probe_done'](tok)
        self.assertIsNone(e['_YT_CB']['probe'])
    def test_probe_outcome_by_holder_releases_and_applies(self):
        e = env_for()
        open_breaker(e)
        e['__ft'].now += 1201                                # cooldown expired
        route, tok = e['_yt_cb_route']()
        self.assertEqual(route, 'probe')
        e['_yt_cb_record'](ok=True, token=tok)
        self.assertIsNone(e['_YT_CB']['probe'])
        self.assertEqual(e['_YT_CB']['fails'], 0)
        self.assertEqual(e['_yt_cb_route']()[0], 'primary')

class Concurrency(unittest.TestCase):
    def test_route_race_yields_exactly_one_probe(self):
        e = env_for()
        open_breaker(e)
        e['__ft'].now += 1201
        results, rlock = [], threading.Lock()
        barrier = threading.Barrier(32)
        def worker():
            barrier.wait()
            r = e['_yt_cb_route']()                  # the routing itself races
            with rlock:
                results.append(r)                    # only the append is serialized
        ts = [threading.Thread(target=worker) for _ in range(32)]
        for t in ts: t.start()
        for t in ts: t.join()
        probes = [tok for route, tok in results if route == 'probe']
        self.assertEqual(len(probes), 1)                     # exactly one half-open probe
        self.assertEqual(sum(1 for route, _ in results if route == 'tv'), 31)
        e['_yt_cb_record'](ok=True, token=probes[0])         # holder closes the breaker
        self.assertEqual(e['_yt_cb_route']()[0], 'primary')
    def test_claim_probe_race_yields_exactly_one_token(self):
        e = env_for()
        open_breaker(e)                                      # cooldown active, probe free
        results, rlock = [], threading.Lock()
        barrier = threading.Barrier(32)
        def worker():
            barrier.wait()
            r = e['_yt_cb_claim_probe']()            # the claim itself races
            with rlock:
                results.append(r)                    # only the append is serialized
        ts = [threading.Thread(target=worker) for _ in range(32)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(sum(1 for r in results if r is not None), 1)
        self.assertEqual(sum(1 for r in results if r is None), 31)

class MediaSnapshot(unittest.TestCase):
    def test_new_and_changed_detected_stale_rejected(self):
        fs = FakeFs()
        fs.write('/tmp/x.mp3')                               # exists before the attempt
        e = env_for(fs=fs)
        with patch_modules(glob=glob_mod(fs)):
            snap = e['_yt_snap']('/tmp/x.%(ext)s')
            self.assertEqual(e['_yt_media_new']('/tmp/x.%(ext)s', snap), [])   # unchanged: not credited
            fs.write('/tmp/y.webm')                              # new file, other template
            fs.write('/tmp/x.mp3')                               # changed (mtime moved)
            self.assertEqual(e['_yt_media_new']('/tmp/x.%(ext)s', snap), ['/tmp/x.mp3'])
            self.assertEqual(e['_yt_media_new']('/tmp/y.%(ext)s', {}), ['/tmp/y.webm'])
    def test_attempt_cleanup_removes_only_attempt_artifacts(self):
        fs = FakeFs()
        fs.write('/tmp/x.mp3')                               # pre-existing: must survive
        e = env_for(fs=fs)
        with patch_modules(glob=glob_mod(fs)):
            snap = e['_yt_snap']('/tmp/x.%(ext)s')
            fs.write('/tmp/x.webm')                          # attempt artifact, other extension
            fs.write('/tmp/x.mp3.part')                      # partial artifact
            e['_yt_attempt_cleanup']('/tmp/x.%(ext)s', snap)
            self.assertEqual(sorted(fs.files), ['/tmp/x.mp3'])
            self.assertEqual(fs.files['/tmp/x.mp3'], list(snap['/tmp/x.mp3']))  # untouched (same sig as the snapshot)

class DownloadFlow(unittest.TestCase):
    def test_cooldown_tv_success_skips_primary(self):
        fs = FakeFs()
        calls = []
        def tv(*a, **k):
            calls.append('tv')
            fs.write('/tmp/x.mp3')
            return ('שיר', 60)
        e = env_for(extra={'yt_download_tv': tv, '_ytdlp_serial': serial_ok(fs, calls)}, fs=fs)
        open_breaker(e)
        with patch_modules(glob=glob_mod(fs)):
            self.assertEqual(e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s'), ('שיר', 60))
        self.assertEqual(calls, ['tv'])
    def test_recovery_probe_success_after_expiry(self):
        fs = FakeFs()
        e = env_for(extra={'_ytdlp_serial': serial_ok(fs)}, fs=fs)
        open_breaker(e)
        e['__ft'].now += 1201
        with patch_modules(glob=glob_mod(fs)):
            self.assertEqual(e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s'), ('x', 60))
        self.assertEqual(e['_YT_CB']['fails'], 0)
        self.assertIsNone(e['_YT_CB']['probe'])
        self.assertEqual(e['_yt_cb_route']()[0], 'primary')
    def test_tv_failure_then_primary_probe_success_closes_breaker(self):
        fs = FakeFs()
        def tv(*a, **k):
            raise RuntimeError('tv down')
        e = env_for(extra={'yt_download_tv': tv, '_ytdlp_serial': serial_ok(fs)}, fs=fs)
        open_breaker(e)                                      # cooldown still active
        with patch_modules(glob=glob_mod(fs)):
            self.assertEqual(e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s'), ('x', 60))
        self.assertEqual(e['_YT_CB']['fails'], 0)
        self.assertEqual(e['_YT_CB']['open_until'], 0.0)
        self.assertIsNone(e['_YT_CB']['probe'])
        self.assertEqual(e['_yt_cb_route']()[0], 'primary')
    def test_tv_leftover_other_extension_removed_before_probe(self):
        # maintainer repro: failed TV writes x.mp4, probe primary writes x.webm; the
        # downstream sorted glob must never see the failed attempt's x.mp4
        fs = FakeFs()
        def tv(*a, **k):
            fs.write('/tmp/x.mp4')
            raise RuntimeError('tv down')
        e = env_for(extra={'yt_download_tv': tv,
                           '_ytdlp_serial': serial_ok(fs, path='/tmp/x.webm')}, fs=fs)
        open_breaker(e)
        with patch_modules(glob=glob_mod(fs)):
            self.assertEqual(e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s'), ('x', 60))
        self.assertNotIn('/tmp/x.mp4', fs.files)             # cleaned before the probe ran
        self.assertIn('/tmp/x.webm', fs.files)
    def test_tv_failure_during_cooldown_single_bounded_probe(self):
        fs = FakeFs()
        calls = []
        e = env_for(extra={'_ytdlp_serial': serial_fail(Exception('HTTP Error 403: Forbidden'), calls)}, fs=fs)
        open_breaker(e)
        with patch_modules(glob=glob_mod(fs)):
            with self.assertRaises(RuntimeError):            # TV stub fails, probe fails: unavailable, no loop
                e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s')
        self.assertEqual(calls, ['primary'])                 # exactly one primary probe
        self.assertTrue(e['__ft'].now < e['_YT_CB']['open_until'])  # breaker re-opened
        self.assertIsNone(e['_YT_CB']['probe'])
    def test_tv_duration_cap_neutral_no_probe(self):
        fs = FakeFs()
        calls = []
        def tv(*a, **k):
            raise e['SongTooLongError']('song too long')     # the real TV cap exception
        e = env_for(extra={'yt_download_tv': tv, '_ytdlp_serial': serial_ok(fs, calls)}, fs=fs)
        open_breaker(e)
        with patch_modules(glob=glob_mod(fs)):
            with self.assertRaises(e['SongTooLongError']):
                e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s')
        self.assertEqual(calls, [])                          # no needless primary probe
        self.assertIsNone(e['_YT_CB']['probe'])              # probe slot never claimed
        self.assertEqual(e['_YT_CB']['fails'], 2)            # streak untouched: cap is neutral
    def test_success_recorded_only_after_media_validation(self):
        fs = FakeFs()
        e = env_for(extra={'_ytdlp_serial': serial_ok(fs)}, fs=fs)
        with patch_modules(glob=glob_mod(fs)):
            self.assertEqual(e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s'), ('x', 60))
        self.assertEqual(e['_YT_CB']['fails'], 0)
        fs2 = FakeFs()                                       # metadata ok but no media produced
        def serial_no_write(yt_dlp, opts, video_id, gate=None, on_error=None):
            if gate is not None:
                gate()
            return {'title': 'x', 'duration': 60}
        e2 = env_for(extra={'_ytdlp_serial': serial_no_write}, fs=fs2)
        with patch_modules(glob=glob_mod(fs2)):
            with self.assertRaises(RuntimeError):            # falls to TV stub -> unavailable
                e2['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s')
        self.assertEqual(e2['_YT_CB']['fails'], 0)           # non-403 failure does not open cooldown
    def test_stale_artifact_from_before_call_not_credited(self):
        fs = FakeFs()
        fs.write('/tmp/x.mp3')                               # leftover from before this call
        def serial_no_write(yt_dlp, opts, video_id, gate=None, on_error=None):
            if gate is not None:
                gate()
            return {'title': 'x', 'duration': 60}
        e = env_for(extra={'_ytdlp_serial': serial_no_write}, fs=fs)
        with patch_modules(glob=glob_mod(fs)):
            with self.assertRaises(RuntimeError):            # no new media: not a success
                e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s')
        self.assertIn('/tmp/x.mp3', fs.files)                # pre-existing file not deleted either
        self.assertEqual(e['_YT_CB']['fails'], 0)
    def test_tv_leftover_not_credited_to_probe(self):
        fs = FakeFs()
        def tv(*a, **k):
            fs.write('/tmp/x.mp3')                           # TV writes media, then fails anyway
            raise RuntimeError('tv down')
        def serial_no_write(yt_dlp, opts, video_id, gate=None, on_error=None):
            if gate is not None:
                gate()
            return {'title': 'x', 'duration': 60}
        e = env_for(extra={'yt_download_tv': tv, '_ytdlp_serial': serial_no_write}, fs=fs)
        open_breaker(e)
        with patch_modules(glob=glob_mod(fs)):
            with self.assertRaises(RuntimeError):            # probe produced nothing: no false success
                e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s')
        self.assertIsNone(e['_YT_CB']['probe'])
    def test_cancel_and_duration_limit_neutral_during_probe(self):
        fs = FakeFs()
        e = env_for(fs=fs)
        open_breaker(e)
        e['__ft'].now += 1201                                # expired: next caller becomes the probe
        e['_ytdlp_serial'] = serial_fail(e['YtCancelled']('cancelled'), cancelled=True)
        with patch_modules(glob=glob_mod(fs)):
            with self.assertRaises(e['YtCancelled']):
                e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s')
        self.assertEqual(e['_YT_CB']['fails'], 2)            # streak untouched
        self.assertIsNone(e['_YT_CB']['probe'])              # probe slot freed by the holder
        route, tok = e['_yt_cb_route']()
        self.assertEqual(route, 'probe')                     # a fresh probe is available at once
        e['_yt_cb_probe_done'](tok)
        e['_ytdlp_serial'] = serial_fail(e['SongTooLongError']('too long'))
        with patch_modules(glob=glob_mod(fs)):
            with self.assertRaises(e['SongTooLongError']):
                e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s')
        self.assertEqual(e['_YT_CB']['fails'], 2)            # duration cap neutral too

class GateAdmission(unittest.TestCase):
    def test_queued_primary_diverts_to_tv_when_breaker_opens(self):
        # maintainer repro: route chosen before the download lock; breaker opens while
        # queued; the admission recheck under the real lock must divert to TV instead of
        # running (and possibly succeeding on) a stale primary that would reset the breaker
        fs = FakeFs()
        calls = []
        def tv(*a, **k):
            calls.append('tv')
            fs.write('/tmp/x.webm')
            return ('שיר', 60)
        e = env_for(extra={'yt_download_tv': tv}, fs=fs)
        def headroom_opens_breaker(*a, **k):
            open_breaker(e)   # another caller's consecutive 403s land while this call waits
        e['_yt_wait_headroom'] = headroom_opens_breaker
        with patch_modules(glob=glob_mod(fs), **ytdlp_mods(FakeYDL)):
            self.assertEqual(e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s'), ('שיר', 60))
        self.assertEqual(calls, ['tv'])                      # primary download never ran
        self.assertEqual(e['_YT_CB']['fails'], 2)            # diversion neutral: breaker not reset
        self.assertTrue(e['__ft'].now < e['_YT_CB']['open_until'])
        self.assertEqual(e['_yt_active'], {})                # no leaked active entry
    def test_gate_passes_for_probe_holder(self):
        fs = FakeFs()
        e = env_for(extra={'_ytdlp_serial': serial_ok(fs)}, fs=fs)
        open_breaker(e)
        e['__ft'].now += 1201
        with patch_modules(glob=glob_mod(fs), **ytdlp_mods(FakeYDL)):
            self.assertEqual(e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s'), ('x', 60))
        self.assertEqual(e['_YT_CB']['fails'], 0)

class SynchronizedRecord(unittest.TestCase):
    def test_on_error_runs_genuinely_under_download_lock(self):
        # deterministic: inside the callback the lock is held by THIS thread, so a
        # non-blocking acquire must fail - no sleeps, no luck
        e = env_for()
        held = []
        def on_error(err):
            held.append(not e['_yt_dl_lock'].acquire(blocking=False))
        class YDL:
            def __init__(self, opts): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download=True):
                raise Exception('HTTP Error 403: Forbidden')
        fake_ytdlp = ytdlp_mods(YDL)['yt_dlp']
        with self.assertRaises(Exception):
            e['_ytdlp_serial'](fake_ytdlp, {}, 'ABCDEFGHIJK', on_error=on_error)
        self.assertEqual(held, [True])
    def test_gate_runs_genuinely_under_download_lock(self):
        e = env_for()
        held = []
        def gate():
            held.append(not e['_yt_dl_lock'].acquire(blocking=False))
        class YDL:
            def __init__(self, opts): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download=True):
                return {'title': 'x', 'duration': 60}
        fake_ytdlp = ytdlp_mods(YDL)['yt_dlp']
        e['_ytdlp_serial'](fake_ytdlp, {}, 'ABCDEFGHIJK', gate=gate)
        self.assertEqual(held, [True])

class MultiRuntimeChain(unittest.TestCase):
    def _chain_env(self, fs, attempts):
        """attempts: list of ('fail', err, solver_failed) or ('ok',); two runtimes."""
        state = {'n': 0}
        def serial(yt_dlp, opts, video_id, gate=None, on_error=None):
            if gate is not None:
                gate()
            spec = attempts[min(state['n'], len(attempts) - 1)]
            state['n'] += 1
            if spec[0] == 'ok':
                fs.write('/tmp/x.mp3')
                return {'title': 'x', 'duration': 60}
            _, err, solver_failed = spec
            if solver_failed:
                opts['logger'].solver_failed = True
            if on_error is not None:
                on_error(err)
            raise err
        e = env_for(extra={'_ytdlp_serial': serial,
                           '_yt_runtime_choices': lambda: ['qjs', 'deno']}, fs=fs)
        return e, state
    def test_probe_intermediate_solver_failure_then_success_closes_breaker(self):
        # maintainer repro: probe runtime1 solver-crashes (non-403), runtime2 succeeds.
        # The intermediate failure must NOT free the probe or break the streak; the
        # success must settle with the still-valid token and close the breaker.
        fs = FakeFs()
        e, state = self._chain_env(fs, [('fail', RuntimeError('solver crashed'), True), ('ok',)])
        open_breaker(e)                                      # cooldown active
        with patch_modules(glob=glob_mod(fs)):
            self.assertEqual(e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s'), ('x', 60))
        self.assertEqual(state['n'], 2)                      # both runtimes tried
        self.assertEqual(e['_YT_CB']['fails'], 0)
        self.assertEqual(e['_YT_CB']['open_until'], 0.0)     # cooldown actually closed
        self.assertIsNone(e['_YT_CB']['probe'])
        self.assertEqual(e['_yt_cb_route']()[0], 'primary')
    def test_intermediate_solver_failure_then_final_403_counted_once(self):
        fs = FakeFs()
        e, state = self._chain_env(fs, [('fail', RuntimeError('solver crashed'), True),
                                        ('fail', Exception('HTTP Error 403: Forbidden'), False)])
        with patch_modules(glob=glob_mod(fs)):
            with self.assertRaises(RuntimeError):            # non-authed TV stub -> unavailable
                e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s')
        self.assertEqual(state['n'], 2)
        self.assertEqual(e['_YT_CB']['fails'], 1)            # only the real 403 counted
    def test_intermediate_403_then_retry_success_resets(self):
        # maintainer's exact repro: streak 1; runtime1 writes x.mp4, solver-fails with a
        # 403 (counted at once: streak 2, cooldown opens); the permitted runtime2 retry
        # must STILL run (admission gate is for the chain's first attempt only), must
        # clean runtime1's artifact, and must not divert to TV. Final success is
        # authoritative: it closes the cooldown the intermediate 403 opened.
        fs = FakeFs()
        state = {'n': 0}
        calls, tv_calls = [], []
        def serial(yt_dlp, opts, video_id, gate=None, on_error=None):
            if gate is not None:
                gate()
            state['n'] += 1
            calls.append('primary%d' % state['n'])
            if state['n'] == 1:
                fs.write('/tmp/x.mp4')                       # failed attempt's artifact
                opts['logger'].solver_failed = True
                err = Exception('HTTP Error 403: Forbidden')
                if on_error is not None:
                    on_error(err)
                raise err
            fs.write('/tmp/x.webm')                          # retry's own artifact
            return {'title': 'x', 'duration': 60}
        def tv(*a, **k):
            tv_calls.append('tv')
            fs.write('/tmp/x.m4a')
            return ('\u05e9\u05d9\u05e8', 60)
        e = env_for(extra={'_ytdlp_serial': serial, 'yt_download_tv': tv,
                           '_yt_runtime_choices': lambda: ['qjs', 'deno']}, fs=fs)
        e['_yt_cb_record_403']()                             # streak 1 before the call
        with patch_modules(glob=glob_mod(fs)):
            self.assertEqual(e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s'), ('x', 60))
        self.assertEqual(calls, ['primary1', 'primary2'])    # permitted retry preserved
        self.assertEqual(tv_calls, [])                       # NO diversion to TV mid-chain
        self.assertNotIn('/tmp/x.mp4', fs.files)             # failed attempt's artifact cleaned
        self.assertEqual(sorted(fs.files), ['/tmp/x.webm'])  # downstream glob sees only the real output
        self.assertEqual(e['_YT_CB']['fails'], 0)
        self.assertEqual(e['_YT_CB']['open_until'], 0.0)     # cooldown the attempt-0 403 opened is closed
    def test_cancel_on_second_attempt_neutral(self):
        fs = FakeFs()
        e, state = self._chain_env(fs, [('fail', RuntimeError('solver crashed'), True),
                                        ('fail', None, False)])
        # second attempt raises YtCancelled through the ev path: simulate via cancelled serial
        e2_cancel = e['YtCancelled']('cancelled')
        state['n'] = 0
        attempts = [('fail', RuntimeError('solver crashed'), True)]
        def serial(yt_dlp, opts, video_id, gate=None, on_error=None):
            if gate is not None:
                gate()
            if state['n'] == 0:
                state['n'] += 1
                opts['logger'].solver_failed = True
                if on_error is not None:
                    on_error(RuntimeError('solver crashed'))
                raise RuntimeError('solver crashed')
            raise e2_cancel                                 # ev-path cancel: no on_error (real serial skips it)
        e['_ytdlp_serial'] = serial
        open_breaker(e)
        e['__ft'].now += 1201                                # expired: this call is the probe
        with patch_modules(glob=glob_mod(fs)):
            with self.assertRaises(e['YtCancelled']):
                e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s')
        self.assertEqual(e['_YT_CB']['fails'], 2)            # untouched
        self.assertIsNone(e['_YT_CB']['probe'])              # released by the holder, neutrally
    def test_cap_on_second_attempt_neutral(self):
        fs = FakeFs()
        e, state = self._chain_env(fs, [('fail', RuntimeError('solver crashed'), True), ('ok',)])
        toolong = e['SongTooLongError']('too long')
        def serial(yt_dlp, opts, video_id, gate=None, on_error=None):
            if gate is not None:
                gate()
            if state['n'] == 0:
                state['n'] += 1
                opts['logger'].solver_failed = True
                if on_error is not None:
                    on_error(RuntimeError('solver crashed'))
                raise RuntimeError('solver crashed')
            if on_error is not None:
                on_error(toolong)                            # real serial calls it; closure must ignore non-403
            raise toolong
        e['_ytdlp_serial'] = serial
        open_breaker(e)
        e['__ft'].now += 1201
        with patch_modules(glob=glob_mod(fs)):
            with self.assertRaises(e['SongTooLongError']):
                e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s')
        self.assertEqual(e['_YT_CB']['fails'], 2)            # cap neutral
        self.assertIsNone(e['_YT_CB']['probe'])

class UnifiedLifecycle(unittest.TestCase):
    def _divert_env(self, fs, tv, serial, calls):
        """Breaker opens (via the headroom hook) while a primary-routed call waits."""
        e = env_for(extra={'yt_download_tv': tv, '_ytdlp_serial': serial}, fs=fs)
        def headroom_opens_breaker(*a, **k):
            open_breaker(e)   # another caller's consecutive 403s land while this call waits
        e['_yt_wait_headroom'] = headroom_opens_breaker
        return e
    def test_diverted_call_tv_failure_gets_probe_recovery(self):
        # gate diversion must join the SAME lifecycle as a TV-routed call: validated TV,
        # then exactly one bounded primary probe when TV fails - not a bare TV return
        fs = FakeFs()
        calls = []
        def tv(*a, **k):
            calls.append('tv')
            raise RuntimeError('tv down')
        e = self._divert_env(fs, tv, serial_ok(fs, calls), calls)
        with patch_modules(glob=glob_mod(fs), **ytdlp_mods(FakeYDL)):
            self.assertEqual(e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s'), ('x', 60))
        self.assertEqual(calls, ['tv', 'primary'])           # TV tried, then the probe ran
        self.assertEqual(e['_YT_CB']['fails'], 0)            # probe success closed the breaker
        self.assertIsNone(e['_YT_CB']['probe'])
    def test_diverted_call_tv_phantom_media_not_accepted(self):
        # TV returns metadata but writes nothing: not a success; the probe must run
        fs = FakeFs()
        calls = []
        def tv(*a, **k):
            calls.append('tv')
            return ('\u05e9\u05d9\u05e8', 60)                      # phantom: no file produced
        e = self._divert_env(fs, tv, serial_ok(fs, calls), calls)
        with patch_modules(glob=glob_mod(fs), **ytdlp_mods(FakeYDL)):
            self.assertEqual(e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s'), ('x', 60))
        self.assertEqual(calls, ['tv', 'primary'])
        self.assertEqual(e['_YT_CB']['fails'], 0)
    def test_synchronized_record_under_lock_before_next_waiter(self):
        # A's 403 is recorded while A still holds the download lock, so the queued B's
        # gate sees the open breaker and never runs a stale primary
        fs = FakeFs()
        a_can_fail = threading.Event()
        a_inside = threading.Event()
        b_extract = []
        tv_calls = []
        def tv(*a, **k):
            tv_calls.append(1)
            fs.write('/tmp/x.webm')
            return ('\u05e9\u05d9\u05e8', 60)
        class YDL:
            def __init__(self, opts): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download=True):
                if threading.current_thread().name == 'A':
                    a_inside.set()
                    a_can_fail.wait(5)
                    raise Exception('HTTP Error 403: Forbidden')
                b_extract.append(1)
                return {'title': 'x', 'duration': 60}
        e = env_for(extra={'yt_download_tv': tv}, fs=fs)     # real _ytdlp_serial for both
        e['_yt_cb_record_403']()                             # streak 1
        results = {}
        def call(name):
            try:
                results[name] = ('ok', e['yt_download']('ABCDEFGHIJK', '/tmp/x.%(ext)s'))
            except Exception as ex:
                results[name] = ('err', type(ex).__name__)
        with patch_modules(glob=glob_mod(fs), **ytdlp_mods(YDL)):
            ta = threading.Thread(target=call, args=('A',), name='A')
            ta.start()
            self.assertTrue(a_inside.wait(2))                # A holds the lock, download in flight
            tb = threading.Thread(target=call, args=('B',), name='B')
            tb.start()
            real_time.sleep(0.3)                             # B queues on the download lock
            a_can_fail.set()                                 # A's primary now 403s (streak 2, cooldown)
            ta.join(5)
            tb.join(5)
        self.assertEqual(results['A'][0], 'ok')              # A recovered via its TV fallback
        self.assertEqual(results['B'], ('ok', ('\u05e9\u05d9\u05e8', 60)))  # B diverted to TV
        self.assertEqual(b_extract, [])                      # B never ran a stale primary
        self.assertEqual(e['_YT_CB']['fails'], 2)            # breaker state intact, not reset by B
        self.assertEqual(len(tv_calls), 2)

class TvRouteMechanics(unittest.TestCase):
    def test_cancel_during_tv_metadata_aborts_before_download(self):
        # registered in _yt_active before yt_player: a cancel while the (slow) metadata
        # call is in flight aborts before any download starts
        meta_entered = threading.Event()
        meta_release = threading.Event()
        constructed = []
        def player(vid):
            meta_entered.set()
            meta_release.wait(5)
            return ('t', 60, 'OK')
        class YDL:
            def __init__(self, opts): constructed.append(1)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def download(self, urls): pass
        e = env_for(extra={'yt_player': player}, real_tv=True)
        outcome = []
        def worker():
            try:
                e['yt_download_tv']('ABCDEFGHIJK', '/tmp/x.%(ext)s')
                outcome.append('returned')
            except Exception as ex:
                outcome.append(type(ex).__name__)
        with patch_modules(**ytdlp_mods(YDL)):
            t = threading.Thread(target=worker)
            t.start()
            self.assertTrue(meta_entered.wait(2))
            e['cancel_yt_except']('ZZZZZZZZZZZ')             # user picked another song mid-metadata
            meta_release.set()
            t.join(5)
        self.assertEqual(outcome, ['YtCancelled'])
        self.assertEqual(constructed, [])                    # download never started after cancel
        self.assertEqual(e['_yt_active'], {})
    def test_tv_duration_cap_raises_song_too_long(self):
        e = env_for(extra={'yt_player': lambda vid: ('t', 900, 'OK')}, real_tv=True)
        with patch_modules(**ytdlp_mods(FakeYDL)):
            with self.assertRaises(e['SongTooLongError']):   # real cap exception, not plain ValueError
                e['yt_download_tv']('ABCDEFGHIJK', '/tmp/x.%(ext)s')
    def test_tv_serialized_on_shared_download_lock(self):
        entered = threading.Event()
        started = threading.Event()
        FakeYDL.on_enter = entered.set
        FakeYDL.on_download = lambda opts, urls: None
        try:
            e = env_for(real_tv=True)
            e['_yt_dl_lock'].acquire()                       # another download holds the lock
            done = []
            def worker():
                started.set()
                done.append(e['yt_download_tv']('ABCDEFGHIJK', '/tmp/x.%(ext)s'))
            with patch_modules(**ytdlp_mods(FakeYDL)):
                t = threading.Thread(target=worker)
                t.start()
                self.assertTrue(started.wait(2))
                real_time.sleep(0.3)
                self.assertFalse(entered.is_set())           # TV blocked behind the held lock
                e['_yt_dl_lock'].release()
                t.join(5)
            self.assertTrue(entered.is_set())
            self.assertEqual(done, [('t', 60)])
        finally:
            FakeYDL.on_enter = None
            FakeYDL.on_download = None
    def test_tv_cancellable_via_cancel_yt_except(self):
        entered = threading.Event()
        proceed = threading.Event()
        FakeYDL.on_enter = lambda: (entered.set(), proceed.wait(5))
        FakeYDL.on_download = lambda opts, urls: [h({}) for h in opts['progress_hooks']]
        try:
            e = env_for(real_tv=True)
            outcome = []
            def worker():
                try:
                    e['yt_download_tv']('ABCDEFGHIJK', '/tmp/x.%(ext)s')
                    outcome.append('returned')
                except Exception as ex:
                    outcome.append(type(ex).__name__)
            with patch_modules(**ytdlp_mods(FakeYDL)):
                t = threading.Thread(target=worker)
                t.start()
                self.assertTrue(entered.wait(2))
                e['cancel_yt_except']('ZZZZZZZZZZZ')         # user picked another song
                proceed.set()
                t.join(5)
            self.assertEqual(outcome, ['YtCancelled'])
            self.assertEqual(e['_yt_active'], {})            # active entry cleaned up
        finally:
            FakeYDL.on_enter = None
            FakeYDL.on_download = None

if __name__ == '__main__':
    unittest.main()
