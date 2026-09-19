import os, re, io, json, time, wave, asyncio, logging, threading, subprocess
import requests
from flask import Flask, request, Response

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('yemot-ai')

app = Flask(__name__)

YM_SYSTEM = os.environ.get('YM_SYSTEM', '')
YM_PASS = os.environ.get('YM_PASS', '')
YM_TOKEN = f'{YM_SYSTEM}:{YM_PASS}'
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
BRIDGE_SECRET = os.environ.get('BRIDGE_SECRET', '')
GROQ_CHAT_MODEL = os.environ.get('GROQ_CHAT_MODEL', 'openai/gpt-oss-120b')
GROQ_STT_MODEL = os.environ.get('GROQ_STT_MODEL', 'whisper-large-v3-turbo')
YT_CLIENT = os.environ.get('YT_PLAYER_CLIENT', 'android_vr')
YT_REFRESH_TOKEN = os.environ.get('YT_REFRESH_TOKEN', '')
PS4_UA = 'Mozilla/5.0 (PlayStation; PlayStation 4/12.00) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15'
TV_CLIENT_VER = '7.20260916.14.00'
YT_OAUTH_CLIENT_ID = '861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com'
YT_OAUTH_CLIENT_SECRET = 'SboVhoG9s0rNafixCSGGKXAT'
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

def yt_search_video_id(query):
    """Authenticated TV-surface search; returns first video id."""
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
    if not found:
        raise ValueError('no video results')
    return found[0]

def yt_download(video_id, outtmpl):
    """yt-dlp download through the authenticated TV client (PS4 UA) with deno decipher."""
    import yt_dlp
    from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
    _yt_cfg()
    tv = INNERTUBE_CLIENTS['tv']
    tv['INNERTUBE_CONTEXT']['client']['userAgent'] = PS4_UA
    tv['INNERTUBE_CONTEXT']['client']['clientVersion'] = TV_CLIENT_VER
    opts = {
        'format': 'bestaudio/best',
        'outtmpl': outtmpl,
        'quiet': True, 'no_warnings': True, 'noplaylist': True,
        'http_headers': {'Authorization': f'Bearer {_yt_token()}',
                         'X-Goog-Visitor-Id': _YT['vd'],
                         'User-Agent': PS4_UA},
        'extractor_args': {'youtube': {'player_client': ['tv'], 'player_skip': ['webpage', 'configs', 'initial_data']}},
    }
    url = f'https://www.youtube.com/watch?v={video_id}'
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get('duration') and info['duration'] > 600:
            raise ValueError('song too long')
        ydl.download([url])
        return info.get('title') or 'שיר', info.get('duration')
EDGE_VOICE = os.environ.get('EDGE_VOICE', 'he-IL-HilaNeural')
EXT_DIR = os.environ.get('YM_AI_EXT', '/1')          # the api extension folder
IN_DIR = '/AI/in'                                    # caller recordings
HIST_DIR = '/AI/history'                             # per-caller history json (as .txt)
SONG_DIR = os.environ.get('YM_SONG_EXT', '/2')        # the songs api extension folder
MAX_DAILY_TURNS = int(os.environ.get('MAX_DAILY_TURNS', '60'))

YM_API = 'https://www.call2all.co.il/ym/api'
GROQ = 'https://api.groq.com/openai/v1'

SYSTEM_PROMPT = (
    'את עוזרת קולית חמה וחברותית בשם "אוזן", שמדברת עם מתקשרים בקו טלפוני. '
    'כללים קשיחים: '
    '1) עני תמיד בעברית בלבד, בשפה מדוברת וטבעית. '
    '2) תשובות קצרות: משפט אחד עד שלושה משפטים. לעולם לא רשימות, מספור, אימוג׳י, כוכביות או סימנים מיוחדים - הטקסט מוקרא בקול. '
    '3) אם המשתמש נפרד או מבקש לסיים (ביי, להתראות, די, תודה זהו) - התחילי את התשובה במילה BYE: ולאחריה משפט פרידה אחד קצר. '
    '4) אם הבקשה לא ברורה, בקשי שיחזור בשאלה קצרה. '
    '5) את בקו אישי וחברותי - שיחה קלה, לא רשמית.'
)

sessions = {}
stats = {'calls': 0, 'turns': 0, 'started': time.time(), 'errors': 0}
lock = threading.Lock()

# ---------- Yemot API ----------

def ym_get(action, **params):
    params['token'] = YM_TOKEN
    r = requests.get(f'{YM_API}/{action}', params=params, timeout=20)
    r.raise_for_status()
    return r

def ym_p(p):
    return p if p.startswith('ivr2:') else 'ivr2:' + p

def ym_download(path):
    r = ym_get('DownloadFile', path=ym_p(path))
    return r.content

def ym_upload(local_bytes, filename, ym_path):
    r = requests.post(f'{YM_API}/UploadFile',
                      data={'token': YM_TOKEN, 'path': ym_p(ym_path)},
                      files={'file': (filename, local_bytes)}, timeout=60)
    r.raise_for_status()
    j = r.json() if r.headers.get('content-type','').startswith('application/json') else {'raw': r.text[:200]}
    if isinstance(j, dict) and j.get('success') is False:
        raise RuntimeError(f"YM upload rejected: {j.get('message')}")
    return j

def ym_delete(ym_path):
    try:
        return ym_get('FileAction', action='delete', target=ym_p(ym_path)).json()
    except Exception as e:
        log.warning('delete failed %s: %s', ym_path, e)
        return None

def ym_newest_file(ym_dir):
    j = ym_get('GetIVR2Dir', path=ym_p(ym_dir)).json()
    files = j.get('files') or []
    if not files:
        return None
    return ym_dir.rstrip('/') + '/' + sorted(f['name'] for f in files)[-1]

# ---------- History (stored on Yemot as .txt) ----------

def hist_path(phone):
    safe = re.sub(r'\D', '', phone or '') or 'anon'
    return f'{HIST_DIR}/{safe}.txt'

def load_history(phone):
    if not phone:
        return {'summary': '', 'turns': [], 'day': '', 'day_turns': 0}
    try:
        data = ym_download(hist_path(phone)).decode('utf-8')
        h = json.loads(data)
        h.setdefault('summary', ''); h.setdefault('turns', [])
        return h
    except Exception:
        return {'summary': '', 'turns': [], 'day': '', 'day_turns': 0}

def save_history(phone, h):
    if not phone:
        return
    try:
        p = hist_path(phone)
        ym_delete(p)
        ym_upload(json.dumps(h, ensure_ascii=False).encode('utf-8'), safe_name(p), p)
    except Exception as e:
        log.warning('history save failed: %s', e)

def safe_name(p):
    return p.rstrip('/').split('/')[-1]

# ---------- Groq ----------

def groq_stt(wav_bytes):
    r = requests.post(f'{GROQ}/audio/transcriptions',
                      headers={'Authorization': f'Bearer {GROQ_API_KEY}'},
                      files={'file': ('audio.wav', wav_bytes, 'audio/wav')},
                      data={'model': GROQ_STT_MODEL, 'language': 'he', 'response_format': 'json'},
                      timeout=40)
    r.raise_for_status()
    return (r.json().get('text') or '').strip()

def groq_chat(messages, max_tokens=180):
    r = requests.post(f'{GROQ}/chat/completions',
                      headers={'Authorization': f'Bearer {GROQ_API_KEY}', 'Content-Type': 'application/json'},
                      json={'model': GROQ_CHAT_MODEL, 'messages': messages, 'reasoning_effort': 'low',
                            'temperature': 0.7, 'max_tokens': max_tokens},
                      timeout=40)
    r.raise_for_status()
    return r.json()['choices'][0]['message']['content'].strip()

# ---------- TTS ----------

def tts_wav(text):
    import edge_tts
    mp3_path = f'/tmp/tts-{time.time_ns()}.mp3'
    async def gen():
        await edge_tts.Communicate(text, EDGE_VOICE).save(mp3_path)
    asyncio.run(gen())
    import miniaudio
    snd = miniaudio.decode_file(mp3_path, output_format=miniaudio.SampleFormat.SIGNED16,
                                nchannels=1, sample_rate=16000)
    os.remove(mp3_path)
    buf = io.BytesIO()
    w = wave.open(buf, 'wb')
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(bytes(snd.samples))
    w.close()
    return buf.getvalue()

# ---------- Helpers ----------

def text_response(body):
    return Response(body, mimetype='text/plain; charset=utf-8')

def upload_reply(text, call_id, turn):
    wav = tts_wav(text)
    name = f'T{call_id[-6:]}{turn}.wav'
    ym_path = f'{EXT_DIR}/{name}'
    ym_upload(wav, name, ym_path)
    return name, ym_path

def summarize_if_needed(phone, h):
    if len(h['turns']) <= 10:
        return h
    try:
        convo = '\n'.join(f"{'מתקשר' if t[0]=='u' else 'אוזן'}: {t[1]}" for t in h['turns'][:-4])
        summ = groq_chat([{'role': 'system', 'content': 'סכמי בעברית בשניים-שלושה משפטים את השיחה הבאה, בגוף שלישי, כולל נושאים ופתרונות עיקריים.'},
                          {'role': 'user', 'content': (h.get('summary','') + '\n' + convo).strip()}], max_tokens=120)
        h['summary'] = summ
        h['turns'] = h['turns'][-4:]
    except Exception as e:
        log.warning('summarize failed: %s', e)
        h['turns'] = h['turns'][-6:]
    return h

def today():
    return time.strftime('%Y-%m-%d')

# ---------- Endpoints ----------

@app.route('/healthz')
def healthz():
    return {'ok': True, 'uptime_s': int(time.time()-stats['started']),
            'env': {'ym': bool(YM_SYSTEM and YM_PASS), 'groq': bool(GROQ_API_KEY), 'secret': bool(BRIDGE_SECRET)}}

@app.route('/status')
def status():
    with lock:
        return {'stats': stats, 'active_calls': len(sessions)}

@app.route('/setup')
def setup():
    if request.args.get('secret') != BRIDGE_SECRET:
        return 'forbidden', 403
    report = {}
    # root menu greeting lives at /000.wav (played by the root menu extension)
    try:
        ym_upload(tts_wav('ברוכים הבאים! לשיחה עם אוזן, הקישו 1. לשיר מיוטיוב, הקישו 2.'), '000.wav', '/000.wav')
        report['menu_000.wav'] = 'ok'
    except Exception as e:
        report['menu_000.wav'] = f'FAIL: {e}'
    assets = {
        'greeting_new.wav': 'היי! הגעת לקו של אוזן. אני חברה וירטואלית שאפשר פשוט לדבר איתה. אז מה נשמע? דברו אחרי הצליל, ולסיום הקישו סולמית.',
        'greeting_back.wav': 'היי! כיף שחזרת. על מה נדבר הפעם? דברו אחרי הצליל, ולסיום הקישו סולמית.',
        'didnt_hear.wav': 'סליחה, לא שמעתי טוב. אפשר לחזור על זה? דברו אחרי הצליל, ולסיום הקישו סולמית.',
        'error.wav': 'סליחה, הייתה תקלה טכנית. נסו שוב קצת מאוחר יותר. להתראות!',
        'tired.wav': 'וואו, דיברנו היום המון! נגמרו לי הכוחות להיום. נדבר מחר, בסדר? להתראות!',
    }
    for name, text in SONG_PROMPTS.items():
        try:
            ym_upload(tts_wav(text), name + '.wav', f'{SONG_DIR}/{name}.wav')
            report[name] = 'ok'
        except Exception as e:
            report[name] = f'FAIL: {e}'
    for name, text in assets.items():
        try:
            ym_upload(tts_wav(text), name, f'{EXT_DIR}/{name}')
            report[name] = 'ok'
        except Exception as e:
            report[name] = f'FAIL: {e}'
            log.error('setup asset %s failed: %s', name, e)
    # groq self-tests
    try:
        report['groq_chat'] = groq_chat([{'role': 'user', 'content': 'ענה במילה אחת: בסדר'}], max_tokens=64)
    except Exception as e:
        report['groq_chat'] = f'FAIL: {e}'
    try:
        report['groq_stt'] = groq_stt(tts_wav('בדיקה, אחת שתיים שלוש'))
    except Exception as e:
        report['groq_stt'] = f'FAIL: {e}'
    return report

# ---------- YouTube songs (extension 2) ----------

SONG_PROMPTS = {
    'song_ask': 'איזה שיר בא לכם? אמרו את שם השיר, אפשר גם את הזמר. דברו אחרי הצליל, ולסיום הקישו סולמית.',
    'song_searching': 'רגע אחד, אני מחפשת את השיר. זה יכול לקחת חצי דקה.',
    'song_wait': 'עוד ממש קצת, השיר כבר בדרך.',
    'song_notfound': 'סליחה, לא הצלחתי למצוא את השיר הזה. נסו שיר אחר. איזה שיר בא לכם?',
    'song_more': 'איזה עוד שיר בא לכם? אמרו את שם השיר, או נתקו.',
    'song_bye': 'כיף היה! נתראה בשיר הבא. להתראות!',
}
song_jobs = {}

def fetch_song(call_id, query):
    job = song_jobs[call_id]
    tmp = f'/tmp/song-{call_id}'
    try:
        import imageio_ffmpeg, glob as _glob
        if not YT_REFRESH_TOKEN:
            raise ValueError('YT_REFRESH_TOKEN not set')
        m = re.search(r'(?:v=|youtu\.be/|/shorts/)([\w-]{11})', query)
        video_id = m.group(1) if m else yt_search_video_id(query)
        log.info('song search q=%r -> video %s', query[:60], video_id)
        title, _dur = yt_download(video_id, tmp + '.%(ext)s')
        files = sorted(_glob.glob(tmp + '.*'))
        if not files:
            raise ValueError('no file downloaded')
        src = files[0]
        out = tmp + '.wav'
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-y', '-i', src,
                        '-ar', '16000', '-ac', '1', '-f', 'wav', out],
                       check=True, capture_output=True, timeout=120)
        name = 'song' + re.sub(r'\D', '', call_id)[-6:]
        with open(out, 'rb') as f:
            ym_upload(f.read(), name + '.wav', f'{SONG_DIR}/{name}.wav')
        for f_ in _glob.glob(tmp + '.*'):
            try: os.remove(f_)
            except OSError: pass
        job.update(status='ready', title=title, name=name)
        log.info('song ready call=%s title=%s', call_id, title[:60])
    except Exception as e:
        log.warning('song fetch failed call=%s: %s', call_id, e)
        job.update(status='error', err=str(e)[:200])

GOODBYE_WORDS = ('להתראות', 'ביי', 'נתק', 'לנתק', 'תודה ביי', 'די', 'סיום')

@app.route('/yemot-song', methods=['GET', 'POST'])
def yemot_song():
    params = request.values
    if params.get('secret') != BRIDGE_SECRET:
        return 'forbidden', 403
    call_id = params.get('ApiCallId') or str(time.time_ns())
    if params.get('hangup') == 'yes':
        with lock:
            song_jobs.pop(call_id, None)
        return text_response('')

    s_val, turn = None, 0
    for k, v in params.items():
        if re.fullmatch(r'S\d+', k):
            s_val, turn = v, int(k[1:])

    with lock:
        job = song_jobs.setdefault(call_id, {'stage': 'ask', 'status': 'idle', 'started': time.time()})

    if s_val is None:
        return text_response(f'read=f-song_ask=S1,no,record,{IN_DIR},,no')

    try:
        stage = job['stage']

        if stage == 'ask':
            rec_path = s_val if s_val.startswith('/') else f'{IN_DIR}/{s_val}'
            wav = ym_download(rec_path)
            text = groq_stt(wav)
            ym_delete(rec_path)
            log.info('song req call=%s: %s', call_id, (text or '')[:80])
            if not text:
                return text_response(f'read=f-didnt_hear=S{turn+1},no,record,{IN_DIR},,no')
            if any(w in text for w in GOODBYE_WORDS) and len(text) < 25:
                with lock:
                    song_jobs.pop(call_id, None)
                return text_response('id_list_message=f-song_bye')
            job.update(stage='wait', status='working', query=text)
            threading.Thread(target=fetch_song, args=(call_id, text), daemon=True).start()
            return text_response(f'read=f-song_searching=S{turn+1},no,no')

        if stage == 'wait':
            st = job.get('status')
            if st == 'working':
                if time.time() - job.get('started', 0) > 150:
                    job.update(stage='ask', status='idle')
                    return text_response(f'read=f-song_notfound=S{turn+1},no,record,{IN_DIR},,no')
                return text_response(f'read=f-song_wait=S{turn+1},no,no')
            if st == 'error':
                job.update(stage='ask', status='idle')
                return text_response(f'read=f-song_notfound=S{turn+1},no,record,{IN_DIR},,no')
            job['stage'] = 'play'
            return text_response(f"read=f-{job['name']}=S{turn+1},no,no")

        if stage == 'play':
            job.update(stage='ask', status='idle')
            return text_response(f'read=f-song_more=S{turn+1},no,record,{IN_DIR},,no')

        job['stage'] = 'ask'
        return text_response(f'read=f-song_ask=S{turn+1},no,record,{IN_DIR},,no')

    except Exception as e:
        log.exception('song call=%s error: %s', call_id, e)
        return text_response('id_list_message=f-error')

@app.route('/song-test')
def song_test():
    if request.args.get('secret') != BRIDGE_SECRET:
        return 'forbidden', 403
    q = request.args.get('q', 'שלום עליכם')
    t0 = time.time()
    tmp = f'/tmp/stest-{time.time_ns()}'
    try:
        import imageio_ffmpeg, glob as _glob
        if not YT_REFRESH_TOKEN:
            return {'ok': False, 'error': 'YT_REFRESH_TOKEN not set'}
        m = re.search(r'(?:v=|youtu\.be/|/shorts/)([\w-]{11})', q)
        video_id = m.group(1) if m else yt_search_video_id(q)
        t1 = time.time()
        title, dur = yt_download(video_id, tmp + '.%(ext)s')
        info = {'entries': None}
        ent = {'duration': dur, 'title': title}
        log.info('song-test video=%s dl=%.1fs', video_id, time.time() - t1)
        files = sorted(_glob.glob(tmp + '.*'))
        if not files:
            return {'ok': False, 'error': 'no file downloaded', 'title': ent.get('title'), 'duration': ent.get('duration')}
        src = files[0]
        out = tmp + '.wav'
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-y', '-i', src,
                        '-ar', '16000', '-ac', '1', '-f', 'wav', out],
                       check=True, capture_output=True, timeout=120)
        size = os.path.getsize(out)
        for f_ in _glob.glob(tmp + '.*'):
            try: os.remove(f_)
            except OSError: pass
        return {'ok': True, 'title': ent.get('title'), 'duration': ent.get('duration'),
                'wav_bytes': size, 'elapsed_s': round(time.time() - t0, 1)}
    except Exception as e:
        import traceback
        return {'ok': False, 'error': str(e)[:300], 'trace': traceback.format_exc()[-900:], 'elapsed_s': round(time.time() - t0, 1)}

@app.route('/pot-test')
def pot_test():
    if request.args.get('secret') != BRIDGE_SECRET:
        return 'forbidden', 403
    import subprocess as sp
    out = []
    try:
        import urllib.request
        out.append('PING: ' + urllib.request.urlopen('http://127.0.0.1:4416/ping', timeout=10).read().decode()[:200])
    except Exception as e:
        out.append('PING ERR: ' + str(e)[:150])
    try:
        ea = request.args.get('ea', 'fetch_pot=always')
        args = ['/opt/venv/bin/yt-dlp', '-v', '--skip-download',
                '--extractor-args', 'youtube:' + ea]
        js = request.args.get('js')
        if js:
            args += ['--js-runtimes', js]
        args += ['https://www.youtube.com/watch?v=UE29iz8zi34']
        r = sp.run(args,
                   capture_output=True, text=True, timeout=150)
        ver = sp.run(['/opt/venv/bin/yt-dlp', '--version'], capture_output=True, text=True)
        out.append('VERSION: ' + ver.stdout.strip())
        keep = [l for l in (r.stdout + r.stderr).splitlines()
                if any(k in l.lower() for k in ('bgutil', 'pot', 'plugin', 'visitor', 'sign in', 'error', 'warning', 'po token', 'http'))]
        out.append('YTDLP:\n' + '\n'.join(keep[:50]))
    except Exception as e:
        out.append('YTDLP ERR: ' + str(e)[:200])
    return '<pre>' + '\n\n'.join(out) + '</pre>'

@app.route('/yemot', methods=['GET', 'POST'])
def yemot():
    params = request.values
    if params.get('secret') != BRIDGE_SECRET:
        return 'forbidden', 403

    call_id = params.get('ApiCallId') or str(time.time_ns())
    phone = params.get('ApiPhone', '')

    if params.get('hangup') == 'yes':
        with lock:
            sessions.pop(call_id, None)
        log.info('hangup call=%s', call_id)
        return text_response('')

    # find the turn param (S1, S2, ...)
    s_val, turn = None, 0
    for k, v in params.items():
        if re.fullmatch(r'S\d+', k):
            s_val, turn = v, int(k[1:])

    with lock:
        sess = sessions.get(call_id)
        if sess is None:
            h = load_history(phone)
            sess = {'phone': phone, 'hist': h, 'empty': 0}
            sessions[call_id] = sess
            stats['calls'] += 1
    h = sess['hist']

    # --- new call: greeting + first record ---
    if s_val is None:
        if h.get('day') == today() and h.get('day_turns', 0) >= MAX_DAILY_TURNS:
            return text_response('id_list_message=f-tired')
        g = 'greeting_back' if (h.get('summary') or h.get('turns')) else 'greeting_new'
        return text_response(f'read=f-{g}=S1,no,record,{IN_DIR},,no')

    t0 = time.time()
    try:
        # --- resolve + transcribe recording ---
        if s_val.endswith('.wav'):
            rec_path = s_val if s_val.startswith('/') else f'{IN_DIR}/{s_val}'
        else:
            rec_path = ym_newest_file(IN_DIR)
        wav = ym_download('ivr2:' + rec_path if not rec_path.startswith('ivr2:') else rec_path)
        user_text = groq_stt(wav)
        ym_delete('ivr2:' + rec_path if not rec_path.startswith('ivr2:') else rec_path)
        log.info('call=%s turn=%d stt(%.1fs): %s', call_id, turn, time.time()-t0, user_text[:80])

        if not user_text:
            sess['empty'] += 1
            if sess['empty'] >= 2:
                return text_response('id_list_message=f-error')
            return text_response(f'read=f-didnt_hear=S{turn+1},no,record,{IN_DIR},,no')
        sess['empty'] = 0

        # --- daily cap ---
        if h.get('day') != today():
            h['day'] = today(); h['day_turns'] = 0
        h['day_turns'] += 1
        if h['day_turns'] > MAX_DAILY_TURNS:
            save_history(phone, h)
            return text_response('id_list_message=f-tired')

        # --- LLM ---
        msgs = [{'role': 'system', 'content': SYSTEM_PROMPT}]
        if h.get('summary'):
            msgs.append({'role': 'system', 'content': 'רקע משיחות קודמות עם המתקשר הזה: ' + h['summary']})
        for who, txt in h.get('turns', [])[-8:]:
            msgs.append({'role': 'user' if who == 'u' else 'assistant', 'content': txt})
        msgs.append({'role': 'user', 'content': user_text})
        reply = groq_chat(msgs)
        is_bye = reply.upper().startswith('BYE')
        reply_text = re.sub(r'^BYE:?\s*', '', reply, flags=re.I).strip() or 'להתראות!'
        log.info('call=%s turn=%d llm(%.1fs) bye=%s: %s', call_id, turn, time.time()-t0, is_bye, reply_text[:80])

        # --- persist history ---
        h.setdefault('turns', []).append(('u', user_text))
        h['turns'].append(('a', reply_text))
        h = summarize_if_needed(phone, h)
        sess['hist'] = h
        save_history(phone, h)

        # --- synthesize + upload reply ---
        name, ym_path = upload_reply(reply_text, call_id, turn)
        stats['turns'] += 1

        if is_bye:
            with lock:
                sessions.pop(call_id, None)
            return text_response(f'id_list_message=f-{name[:-4]}')
        return text_response(f'read=f-{name[:-4]}=S{turn+1},no,record,{IN_DIR},,no')

    except Exception as e:
        stats['errors'] += 1
        log.exception('turn failed: %s', e)
        return text_response('id_list_message=f-error')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
