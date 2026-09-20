import os, re, io, json, time, wave, asyncio, logging, threading, subprocess, shutil
import requests
import urllib.request
from flask import Flask, request, Response

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('yemot-songs')

app = Flask(__name__)

@app.before_request
def _fix_ym_glued_query():
    """Yemot appends its params with '?' even when api_link already has a query
    string, producing /yemot?secret=XXX?ApiCallId=YYY&... - split it back."""
    try:
        sec = request.args.get('secret')
        if sec and '?' in sec:
            import urllib.parse
            from werkzeug.datastructures import MultiDict
            base, glued = sec.split('?', 1)
            items = [('secret', base)] + urllib.parse.parse_qsl(glued) + \
                    [(k, v) for k, v in request.args.items(multi=True) if k != 'secret']
            request.args = MultiDict(items)
            request.__dict__.pop('values', None)
    except Exception:
        pass

@app.before_request
def _log_req():
    if request.path != '/healthz':
        safe = {k: ('<set>' if k == 'secret' else v) for k, v in request.values.items()}
        log.info('REQ %s %s args=%s', request.method, request.path, safe)

YM_SYSTEM = os.environ.get('YM_SYSTEM', '')
YM_PASS = os.environ.get('YM_PASS', '')
YM_TOKEN = f'{YM_SYSTEM}:{YM_PASS}'
YM_API = 'https://www.call2all.co.il/ym/api'
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
GROQ_STT_MODEL = os.environ.get('GROQ_STT_MODEL', 'whisper-large-v3-turbo')
GROQ = 'https://api.groq.com/openai/v1'
BRIDGE_SECRET = os.environ.get('BRIDGE_SECRET', '')
EXT_DIR = os.environ.get('YM_EXT', '/1')      # api extension folder on the YM system
IN_DIR = '/AI/in'                              # caller recordings

def ym_p(p):
    return p if p.startswith('ivr2:') else 'ivr2:' + p

def ym_get(action, **params):
    params['token'] = YM_TOKEN
    r = requests.get(f'{YM_API}/{action}', params=params, timeout=20)
    r.raise_for_status()
    return r

def ym_download(path):
    return ym_get('DownloadFile', path=ym_p(path)).content

def ym_upload(local_bytes, filename, ym_path):
    r = requests.post(f'{YM_API}/UploadFile',
                      data={'token': YM_TOKEN, 'path': ym_p(ym_path)},
                      files={'file': (filename, local_bytes)}, timeout=120)
    r.raise_for_status()
    j = r.json()
    if isinstance(j, dict) and j.get('success') is False:
        raise RuntimeError(f"YM upload rejected: {j.get('message')}")
    return j

def ym_delete(ym_path):
    try:
        r = requests.get(f'{YM_API}/FileAction',
                         params={'token': YM_TOKEN, 'action': 'delete', 'path': ym_p(ym_path)},
                         timeout=20)
        return r.json()
    except Exception as e:
        log.warning('delete failed %s: %s', ym_path, e)
        return None

def text_response(text):
    return Response(text, mimetype='text/plain; charset=utf-8')

# ---------- Groq STT ----------

def groq_stt(wav_bytes, language='he'):
    r = requests.post(f'{GROQ}/audio/transcriptions',
                      headers={'Authorization': f'Bearer {GROQ_API_KEY}'},
                      files={'file': ('audio.wav', wav_bytes, 'audio/wav')},
                      data={'model': GROQ_STT_MODEL, 'language': language,
                            'response_format': 'json'},
                      timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f'groq stt {r.status_code}: {r.text[:200]}')
    return (r.json().get('text') or '').strip()

# ---------- YouTube (authenticated TV client) ----------

PS4_UA = 'Mozilla/5.0 (PlayStation 4 3.11) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/74.0.3729.157 Safari/537.36'
TV_CLIENT_VER = '7.20260916.14.00'
YT_OAUTH_CLIENT_ID = '861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com'
YT_OAUTH_CLIENT_SECRET = 'SboVhoG9s0rNafixCSGGKXAT'
YT_REFRESH_TOKEN = os.environ.get('YT_REFRESH_TOKEN', '')
_YT = {'at': None, 'at_exp': 0.0, 'key': None, 'vd': None, 'sts': None, 'cfg_at': 0.0}

def _yt_token():
    import json as J, urllib.request as U
    if _YT['at'] and time.time() < _YT['at_exp'] - 120:
        return _YT['at']
    body = J.dumps({'client_id': YT_OAUTH_CLIENT_ID, 'client_secret': YT_OAUTH_CLIENT_SECRET,
                    'grant_type': 'refresh_token', 'refresh_token': YT_REFRESH_TOKEN}).encode()
    r = J.load(U.urlopen(U.Request('https://www.youtube.com/o/oauth2/token', data=body,
                                   headers={'Content-Type': 'application/json'}), timeout=30))
    _YT['at'] = r['access_token']
    _YT['at_exp'] = time.time() + r.get('expires_in', 3600)
    return _YT['at']

def _yt_cfg():
    import re, urllib.request as U
    if _YT['key'] and time.time() < _YT['cfg_at'] + 6 * 3600:
        return
    html = U.urlopen(U.Request('https://www.youtube.com/',
        headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36'}),
        timeout=20).read().decode('utf-8', 'ignore')
    _YT['key'] = re.search(r'"INNERTUBE_API_KEY":"([^"]*)"', html).group(1)
    _YT['vd'] = re.search(r'"VISITOR_DATA":"([^"]*)"', html).group(1)
    m = re.search(r'"STS":(\d+)', html)
    _YT['sts'] = m.group(1) if m else None
    _YT['cfg_at'] = time.time()

def _yt_headers():
    return {'Authorization': f'Bearer {_yt_token()}', 'Content-Type': 'application/json',
            'X-YouTube-Client-Name': '7', 'X-YouTube-Client-Version': TV_CLIENT_VER,
            'X-Goog-Visitor-Id': _YT['vd'], 'User-Agent': PS4_UA}

def _yt_tv_context():
    return {'client': {'clientName': 'TVHTML5', 'clientVersion': TV_CLIENT_VER,
                       'hl': 'en', 'visitorData': _YT['vd']}}

def yt_search_candidates(query):
    import json as J, urllib.request as U
    _yt_cfg()
    body = J.dumps({'context': _yt_tv_context(), 'query': query}).encode()
    r = J.load(U.urlopen(U.Request(
        f'https://www.youtube.com/youtubei/v1/search?prettyPrint=false&key={_YT["key"]}',
        data=body, headers=_yt_headers()), timeout=30))
    found = []
    def walk(o):
        if isinstance(o, dict):
            lv = o.get('lockupViewModel')
            if lv and 'VIDEO' in str(lv.get('contentType', '')) and lv.get('contentId'):
                found.append(lv['contentId'])
            vr = o.get('videoRenderer')
            if vr and vr.get('videoId'):
                found.append(vr['videoId'])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(r)
    seen, out = set(), []
    for v in found:
        if v not in seen:
            seen.add(v); out.append(v)
    if not out:
        raise ValueError('no video results')
    return out

def yt_player(video_id):
    import json as J, urllib.request as U
    _yt_cfg()
    body = {'context': _yt_tv_context(), 'videoId': video_id, 'params': '2AMB',
            'contentCheckOk': True, 'racyCheckOk': True}
    if _YT['sts']:
        body['playbackContext'] = {'contentPlaybackContext': {
            'html5Preference': 'HTML5_PREF_WANTS', 'signatureTimestamp': int(_YT['sts'])}}
    r = J.load(U.urlopen(U.Request(
        f'https://www.youtube.com/youtubei/v1/player?prettyPrint=false&key={_YT["key"]}',
        data=J.dumps(body).encode(), headers=_yt_headers()), timeout=30))
    ps = r.get('playabilityStatus', {})
    vd = r.get('videoDetails') or {}
    return vd.get('title') or 'שיר', vd.get('lengthSeconds'), ps.get('status')

def yt_download(video_id, outtmpl):
    import yt_dlp
    from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
    title, duration, status = yt_player(video_id)
    if status != 'OK':
        raise ValueError(f'player status {status}')
    if duration and int(duration) > 600:
        raise ValueError('song too long')
    tv = INNERTUBE_CLIENTS['tv']
    tv['INNERTUBE_CONTEXT']['client']['userAgent'] = PS4_UA
    tv['INNERTUBE_CONTEXT']['client']['clientVersion'] = TV_CLIENT_VER
    opts = {
        'format': '18/bestaudio/best',
        'outtmpl': outtmpl,
        'quiet': True, 'no_warnings': True, 'noplaylist': True,
        'remote_components': ['ejs:github'],
        'http_headers': {'Authorization': f'Bearer {_yt_token()}',
                         'User-Agent': PS4_UA},
        'extractor_args': {'youtube': {'player_client': ['tv'],
                                       'player_skip': ['webpage', 'configs', 'initial_data']}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download(['https://www.youtube.com/watch?v=' + video_id])
    return title, duration

# ---------- Song flow ----------

GOODBYE_WORDS = ('להתראות', 'ביי', 'נתק', 'לנתק', 'די', 'סיום', 'תודה')
jobs = {}
lock = threading.Lock()

def fetch_song(call_id):
    job = jobs[call_id]
    tmp = f'/tmp/song-{call_id}'
    try:
        import imageio_ffmpeg, glob as _glob
        if not YT_REFRESH_TOKEN:
            raise ValueError('YT_REFRESH_TOKEN not set')
        query = job['query']
        m = re.search(r'(?:v=|youtu\.be/|/shorts/)([\w-]{11})', query)
        if m:
            candidates = [m.group(1)]
        else:
            candidates = yt_search_candidates(query)
        video_id, title = None, None
        for cand in candidates[:6]:
            try:
                t, dur, st = yt_player(cand)
                log.info('cand %s: %s %ss %s', cand, (t or '')[:40], dur, st)
                if st == 'OK' and (not dur or int(dur) <= 600):
                    video_id, title = cand, t
                    break
            except Exception as e:
                log.info('cand %s player failed: %s', cand, e)
        if not video_id:
            raise ValueError('no playable short result')
        log.info('song search q=%r -> video %s', query[:60], video_id)
        title, _dur = yt_download(video_id, tmp + '.%(ext)s')
        files = sorted(_glob.glob(tmp + '.*'))
        if not files:
            raise ValueError('no file downloaded')
        out = tmp + '.wav'
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-y', '-i', files[0],
                        '-ar', '8000', '-ac', '1', '-f', 'wav', out],
                       check=True, capture_output=True, timeout=120)
        old = job.get('name')
        name = 'song' + re.sub(r'\D', '', call_id)[-6:]
        with open(out, 'rb') as f:
            ym_upload(f.read(), name + '.wav', f'{EXT_DIR}/{name}.wav')
        if old and old != name:
            ym_delete(f'{EXT_DIR}/{old}.wav')
        for f_ in _glob.glob(tmp + '.*'):
            try: os.remove(f_)
            except OSError: pass
        job.update(status='ready', title=title, name=name)
        log.info('song ready call=%s title=%s', call_id, title[:60])
    except Exception as e:
        log.warning('song fetch failed call=%s: %s', call_id, e)
        job.update(status='error', err=str(e)[:200])

@app.route('/healthz')
def healthz():
    return {'ok': True, 'v': 'songs-v1'}

@app.route('/yemot', methods=['GET', 'POST'])
def yemot():
    params = request.values
    if params.get('secret') != BRIDGE_SECRET:
        return 'forbidden', 403
    call_id = params.get('ApiCallId') or str(time.time_ns())
    if params.get('hangup') == 'yes':
        with lock:
            jobs.pop(call_id, None)
        log.info('hangup call=%s', call_id)
        return text_response('')

    s_val, turn = None, 0
    for k, v in params.items():
        if re.fullmatch(r'S\d+', k):
            s_val, turn = v, int(k[1:])

    with lock:
        job = jobs.setdefault(call_id, {'stage': 'ask', 'status': 'idle', 'started': time.time()})

    if s_val is None:
        return text_response(f'read=f-ask=S1,no,record,{IN_DIR},,no')

    try:
        stage = job['stage']

        if stage == 'ask':
            rec_path = s_val if s_val.startswith('/') else f'{IN_DIR}/{s_val}'
            wav = ym_download(rec_path)
            if not wav.startswith(b'RIFF'):
                log.warning('rec download not wav (%d bytes), retrying', len(wav))
                time.sleep(2)
                wav = ym_download(rec_path)
            if not wav.startswith(b'RIFF'):
                return text_response(f'read=f-didnthear=S{turn+1},no,record,{IN_DIR},,no')
            ym_delete(rec_path)
            text = groq_stt(wav)
            log.info('req call=%s: %s', call_id, (text or '')[:80])
            if not text:
                return text_response(f'read=f-didnthear=S{turn+1},no,record,{IN_DIR},,no')
            if any(w in text for w in GOODBYE_WORDS) and len(text) < 25:
                with lock:
                    jobs.pop(call_id, None)
                return text_response('id_list_message=f-bye')
            job.update(stage='wait', status='working', started=time.time(), query=text)
            threading.Thread(target=fetch_song, args=(call_id,), daemon=True).start()
            return text_response(f'read=f-searching=S{turn+1},no,no')

        if stage == 'wait':
            st = job.get('status')
            if st == 'working':
                if time.time() - job.get('started', 0) > 150:
                    job.update(stage='ask', status='idle')
                    return text_response(f'read=f-notfound=S{turn+1},no,record,{IN_DIR},,no')
                return text_response(f'read=f-wait=S{turn},no,no')
            if st == 'error':
                job.update(stage='ask', status='idle')
                return text_response(f'read=f-notfound=S{turn+1},no,record,{IN_DIR},,no')
            job['stage'] = 'play'
            return text_response(f"read=f-{job['name']}=S{turn+1},no,no")

        if stage == 'play':
            job['stage'] = 'ask'
            job['status'] = 'idle'
            return text_response(f'read=f-more=S{turn+1},no,record,{IN_DIR},,no')

        job['stage'] = 'ask'
        return text_response(f'read=f-ask=S{turn+1},no,record,{IN_DIR},,no')
    except Exception as e:
        log.warning('call=%s error: %s', call_id, e)
        with lock:
            jobs.pop(call_id, None)
        return text_response('id_list_message=f-bye')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
