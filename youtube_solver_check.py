"""Offline-only runtime/solver readiness. Never reads credentials or YouTube."""
import json
import os
import subprocess
from importlib.metadata import version


def check(runtime=None):
    result = {'ok': False}
    try:
        runtime = runtime or os.environ.get('YT_DENO_PATH', '/opt/venv/bin/deno')
        from yt_dlp_ejs.yt.solver import core, lib
        result['ejs_version'] = version('yt-dlp-ejs')
        if result['ejs_version'] != '0.8.0':
            return result
        env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR') if k in os.environ}
        r = subprocess.run([runtime, '--version'], capture_output=True, text=True,
                           timeout=10, env=env)
        first = r.stdout.splitlines()[0] if r.stdout else ''
        if r.returncode or not first.startswith('deno 2.9.5 '):
            return result
        result['deno_version'] = '2.9.5'
        # Bundled solver code is evaluated offline with Deno's denied default
        # filesystem/network/env permissions. No network fetch or player call.
        script = lib() + '\nconst {meriyah,astring}=lib;\n' + core() + '\nconsole.log("solver-assets-ok")'
        r = subprocess.run([runtime, 'run', '--no-config', '--no-lock', '--no-prompt', '-'],
                           input=script, capture_output=True, text=True, timeout=15, env=env)
        result['ok'] = r.returncode == 0 and r.stdout.strip() == 'solver-assets-ok'
    except Exception:
        pass
    return result


if __name__ == '__main__':
    result = check()
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result['ok'] else 1)
