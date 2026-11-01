"""Credential-free tests for the headroom effective-free diagnostic.
Extracts only the functions under test; no app startup or network."""
import ast
import io
import logging
import re
import types
import unittest
from pathlib import Path

source = Path(__file__).resolve().parents[1].joinpath('app.py').read_text()
tree = ast.parse(source)
NAMES = ('_cg_mb', '_cg_stat_mb', '_yt_free_mb', '_yt_wait_headroom')
functions = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in NAMES:
        node.decorator_list = []
        functions.append(node)

class FakeTime:
    def __init__(self): self.now = 1000.0
    def time(self): return self.now
    def sleep(self, s): self.now += s

class RecLog:
    def __init__(self): self.msgs = []
    def info(self, m, *a): self.msgs.append(m % a if a else m)
    def warning(self, m, *a): pass

def env_for(files, warm=True):
    ft = FakeTime()
    def fake_open(path, *a, **k):
        v = files[path]
        if isinstance(v, list):  # successive reads
            item = v.pop(0)
            if item is None:
                raise FileNotFoundError(path)
            return io.StringIO(item)
        return io.StringIO(v)
    env = {'open': fake_open, 'time': ft, '__ft': ft, 're': re,
           'os': types.SimpleNamespace(path=types.SimpleNamespace(isdir=lambda p: warm, join=lambda *a: '/'.join(a)), listdir=lambda p: ['x']),
           'log': RecLog(), 'YT_CACHE_DIR': '/cache',
           'bool': bool, 'int': int, 'len': len, 'max': max, 'min': min, 'isinstance': isinstance}
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<memtest>', 'exec'), env)
    return env

LIM = str(512 * 1048576)
CUR = str(405 * 1048576)
STAT = 'anon 300\nfile 100\ninactive_file 104857600\nshmem 0\n'  # 100 MB reclaimable

class EffectiveFree(unittest.TestCase):
    def test_reclaimable_counts_as_free(self):
        e = env_for({'/sys/fs/cgroup/memory.stat': STAT})
        self.assertAlmostEqual(e['_yt_free_mb'](512, 405), 207.0, places=1)
    def test_missing_stat_falls_back_to_plain_current(self):
        e = env_for({'/sys/fs/cgroup/memory.max': LIM})  # memory.stat unreadable -> None -> plain free
        self.assertAlmostEqual(e['_yt_free_mb'](512, 405), 107.0, places=1)
    def test_malformed_stat_falls_back(self):
        e = env_for({'/sys/fs/cgroup/memory.stat': 'inactive_file notanumber\n'})
        self.assertAlmostEqual(e['_yt_free_mb'](512, 405), 107.0, places=1)
    def test_negative_inactive_file_rejected(self):
        e = env_for({'/sys/fs/cgroup/memory.stat': 'inactive_file -1048576\n'})
        self.assertAlmostEqual(e['_yt_free_mb'](512, 405), 107.0, places=1)
    def test_inactive_file_never_exceeds_current(self):
        e = env_for({'/sys/fs/cgroup/memory.stat': STAT})
        self.assertAlmostEqual(e['_yt_free_mb'](512, 50), 512.0, places=1)

class HeadroomWait(unittest.TestCase):
    def test_warm_path_no_wait_and_diag_fields(self):
        e = env_for({'/sys/fs/cgroup/memory.max': LIM, '/sys/fs/cgroup/memory.current': CUR,
                     '/sys/fs/cgroup/memory.stat': STAT})
        e['_yt_wait_headroom'](label='qjs')
        self.assertEqual(e['__ft'].now, 1000.0)  # no sleep
        self.assertEqual(len(e['log'].msgs), 1)
        m = e['log'].msgs[0]
        for field in ('waited 0.0s', 'need 150 MB warm', 'runtime qjs', 'current 405', 'inactive_file 100', 'effective free 207 of 512'):
            self.assertIn(field, m)
    def test_cold_path_waits_full_window_then_proceeds(self):
        e = env_for({'/sys/fs/cgroup/memory.max': LIM, '/sys/fs/cgroup/memory.current': CUR,
                     '/sys/fs/cgroup/memory.stat': STAT}, warm=False)
        e['_yt_wait_headroom'](max_wait=8.0)  # cold: need 270 > 207 effective
        self.assertGreaterEqual(e['__ft'].now - 1000.0, 8.0)
        self.assertIn('cold', e['log'].msgs[-1])
    def test_current_read_fails_after_sleep_returns_cleanly(self):
        e = env_for({'/sys/fs/cgroup/memory.max': LIM,
                     '/sys/fs/cgroup/memory.current': [CUR, None],  # first read high, second fails
                     '/sys/fs/cgroup/memory.stat': 'inactive_file 0\n'})
        e['_yt_wait_headroom'](max_wait=8.0)  # must not raise TypeError
        self.assertEqual(e['log'].msgs, [])  # returned before diagnostic
    def test_zero_max_wait_no_crash(self):
        e = env_for({'/sys/fs/cgroup/memory.max': LIM})
        e['_yt_wait_headroom'](max_wait=0)  # loop body never runs; log must not crash on cur None
        self.assertEqual(len(e['log'].msgs), 1)
        self.assertIn('current n/a', e['log'].msgs[0])
    def test_no_cgroup_means_no_wait(self):
        e = env_for({})
        e['_yt_wait_headroom']()
        self.assertEqual(e['__ft'].now, 1000.0)

if __name__ == '__main__':
    unittest.main()
