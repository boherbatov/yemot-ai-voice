import os, re, io, json, time, wave, asyncio, logging, threading, subprocess
import requests
import urllib.request
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
                title = (((lv.get('metadata') or {}).get('lockupMetadataViewModel') or {})
                         .get('title') or {}).get('content')
                found.append((lv['contentId'], title))
            vr = o.get('videoRenderer')
            if vr and vr.get('videoId'):
                t = (vr.get('title') or {}).get('runs', [{}])[0].get('text')
                found.append((vr['videoId'], t))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(r)
    if not found:
        raise ValueError('no video results')
    return found[0]

def yt_player(video_id):
    """Direct authenticated TV player call; returns (title, duration, status)."""
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
    """yt-dlp download through the authenticated TV client (PS4 UA) with deno decipher."""
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
                                       'player_skip': ['webpage', 'configs', 'initial_data'],
                                       'visitor_data': [_YT['vd']]}},
    }
    url = f'https://www.youtube.com/watch?v={video_id}'
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return title, duration
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

def ym_upload_text(text, ym_path):
    r = ym_get('UploadTextFile', path=ym_p(ym_path), contents=text)
    j = r.json()
    if j.get('responseStatus') != 'OK':
        raise RuntimeError(f"UploadTextFile rejected: {j.get('message')}")
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
    for name, text in POD_PROMPTS.items():
        try:
            report[name] = 'OK' if ym_upload(tts_wav(text), name + '.wav', f'/3/{name}.wav') else 'FAIL'
        except Exception as e:
            report[name] = f'FAIL: {e}'
    for name in LIB_PROMPTS:
        try:
            report[name] = 'OK' if ym_upload(tts_wav(SONG_PROMPTS[name]), name + '.wav', f'/5/{name}.wav') else 'FAIL'
        except Exception as e:
            report[name] = f'FAIL: {e}'
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
    'song_after': 'לשמירת השיר ברשימה, הקישו 1. לשיר נוסף, הקישו 2. לסיום, הקישו 3.',
    'song_pick': 'להוספה לרשימה חדשה, הקישו 0. להוספה לרשימה קיימת, הקישו את מספר הרשימה, ואז סולמית.',
    'song_pick_bad': 'אין רשימה עם המספר הזה. הקישו 0 לרשימה חדשה, או מספר של רשימה קיימת, ואז סולמית.',
    'song_saved': 'השיר נשמר ברשימה מספר',
    'song_saved_listen': 'להאזנה לרשימה עכשיו, הקישו 1. לחיפוש שיר נוסף, הקישו 2.',
    'lib_pick': 'הקישו את מספר הרשימה, ואז סולמית. לחזרה לתפריט הראשי, הקישו 0 וסולמית.',
    'lib_bad': 'אין רשימה עם המספר הזה. הקישו מספר רשימה, ואז סולמית. לחזרה לתפריט הראשי, הקישו 0 וסולמית.',
}

LIB_PROMPTS = ('lib_pick', 'lib_bad')

LIB_DIR = os.environ.get('YM_LIB_EXT', '/6')            # playlists root extension

def ym_list_files(path):
    r = ym_get('GetFiles', path=ym_p(path))
    d = r.json()
    if d.get('responseStatus') != 'OK':
        return []
    return d.get('files') or []

def playlist_next_number():
    nums = []
    for f in ym_list_files(f'ivr2:{LIB_DIR}'):
        if f.get('fileType') == 'EXT' and f.get('name', '').isdigit():
            nums.append(int(f['name']))
    return max(nums) + 1 if nums else 1

def playlist_exists(n):
    for f in ym_list_files(f'ivr2:{LIB_DIR}'):
        if f.get('fileType') == 'EXT' and f.get('name') == str(n):
            return True
    return False

def playlist_save(n, song_name, title):
    # copy the song wav from the songs ext into playlist n as the next sequence file
    try:
        seq_files = [f['name'] for f in ym_list_files(f'ivr2:{LIB_DIR}/{n}')
                     if re.fullmatch(r'\d{3}\.wav', f.get('name', ''))]
        seq = max((int(x[:3]) for x in seq_files), default=0) + 1
        if not seq_files:
            ym_upload_text('type=playfile\n', f'ivr2:{LIB_DIR}/{n}/ext.ini')
        wav = ym_download(f'{SONG_DIR}/{song_name}.wav')
        ym_upload(wav, f'{seq:03d}.wav', f'ivr2:{LIB_DIR}/{n}/{seq:03d}.wav')
        titles = {}
        try:
            old = ym_get('DownloadFile', path=ym_p(f'ivr2:{LIB_DIR}/{n}/titles.ini')).text
            for line in old.splitlines():
                if '=' in line:
                    k, v = line.split('=', 1)
                    titles[k] = v
        except Exception:
            pass
        titles[f'{seq:03d}'] = title[:120]
        ym_upload_text(''.join(f'{k}={v}\n' for k, v in sorted(titles.items())),
                       f'ivr2:{LIB_DIR}/{n}/titles.ini')
        return seq
    except Exception as e:
        log.warning('playlist save failed pl=%s: %s', n, e)
        return None
song_jobs = {}

def fetch_song(call_id, query):
    job = song_jobs[call_id]
    tmp = f'/tmp/song-{call_id}'
    try:
        import imageio_ffmpeg, glob as _glob
        if not YT_REFRESH_TOKEN:
            raise ValueError('YT_REFRESH_TOKEN not set')
        m = re.search(r'(?:v=|youtu\.be/|/shorts/)([\w-]{11})', query)
        search_title = None
        if m:
            video_id = m.group(1)
        else:
            video_id, search_title = yt_search_video_id(query)
        log.info('song search q=%r -> video %s', query[:60], video_id)
        title, _dur = yt_download(video_id, tmp + '.%(ext)s')
        if title == 'שיר' and search_title:
            title = search_title
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

@app.route('/ym-admin')
def ym_admin():
    if request.args.get('secret') != BRIDGE_SECRET:
        return 'forbidden', 403
    action = request.args.get('action', 'ReadIniFile')
    path = request.args.get('path', '')
    params = {}
    if action == 'ReadIniFile':
        params['path'] = ym_p(path)
    elif action == 'UpdateExtension':
        params['path'] = ym_p(path)
        for k, v in request.args.items():
            if k not in ('secret', 'action', 'path'):
                params[k] = v
    elif action == 'GetIIVRSettings':
        pass
    else:
        return {'ok': False, 'error': 'unsupported action'}, 400
    try:
        r = ym_get(action, **params)
        try:
            return {'ok': True, 'data': r.json()}
        except Exception:
            return {'ok': True, 'data': r.text[:4000]}
    except Exception as e:
        return {'ok': False, 'error': str(e)[:300]}, 502

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
            job['stage'] = 'after'
            return text_response(f'read=f-song_after=S{turn+1},,1,1,Digits,yes')

        if stage == 'after':
            if s_val == '1':
                job['stage'] = 'save_pick'
                return text_response(f'read=f-song_pick=S{turn+1},,1,2,Digits,yes')
            if s_val == '3':
                with lock:
                    song_jobs.pop(call_id, None)
                return text_response('id_list_message=f-song_bye')
            job.update(stage='ask', status='idle')
            return text_response(f'read=f-song_more=S{turn+1},no,record,{IN_DIR},,no')

        if stage == 'save_pick':
            pl = None
            if s_val == '0':
                pl = playlist_next_number()
            elif s_val and s_val.isdigit() and playlist_exists(int(s_val)):
                pl = int(s_val)
            if pl is None:
                return text_response(f'read=f-song_pick_bad=S{turn+1},,1,2,Digits,yes')
            seq = playlist_save(pl, job['name'], job.get('title', ''))
            if seq is None:
                job.update(stage='ask', status='idle')
                return text_response(f'read=f-error.f-song_more=S{turn+1},no,record,{IN_DIR},,no')
            job.update(stage='saved_listen', playlist=pl)
            return text_response(f'read=f-song_saved.n-{pl}.f-song_saved_listen=S{turn+1},,1,1,Digits,yes')

        if stage == 'saved_listen':
            pl = job.get('playlist')
            if s_val == '1' and pl:
                with lock:
                    song_jobs.pop(call_id, None)
                return text_response(f'go_to_folder={LIB_DIR}/{pl}')
            job.update(stage='ask', status='idle')
            return text_response(f'read=f-song_more=S{turn+1},no,record,{IN_DIR},,no')

        job['stage'] = 'ask'
        return text_response(f'read=f-song_ask=S{turn+1},no,record,{IN_DIR},,no')

    except Exception as e:
        log.exception('song call=%s error: %s', call_id, e)
        return text_response('id_list_message=f-error')

@app.route('/yemot-lib', methods=['GET', 'POST'])
def yemot_lib():
    params = request.values
    if params.get('secret') != BRIDGE_SECRET:
        return 'forbidden', 403
    s_val, turn = None, 0
    for k, v in params.items():
        if re.fullmatch(r'S\d+', k):
            s_val, turn = v, int(k[1:])
    if s_val is None:
        return text_response(f'read=f-lib_pick=S1,,1,2,Digits,yes')
    if s_val == '0' or not s_val.strip():
        return text_response('go_to_folder=/')
    if s_val.isdigit() and playlist_exists(int(s_val)):
        return text_response(f'go_to_folder={LIB_DIR}/{int(s_val)}')
    return text_response(f'read=f-lib_bad=S{turn+1},,1,2,Digits,yes')


# ---------- Podcasts (extension 3) ----------

PODCASTS = [
 {
  "title": "אחד ביום",
  "feed": "https://www.omnycontent.com/d/playlist/2ee97a4e-8795-4260-9648-accf00a38c6a/ac2da21e-2193-4683-bcb5-accf011076ad/409bad89-c4c2-46cb-b69b-accf01152781/podcast.rss"
 },
 {
  "title": "פודקאסט שולחן 4",
  "feed": "https://anchor.fm/s/1047a9180/podcast/rss"
 },
 {
  "title": "לוינסון על הבוקר",
  "feed": "https://anchor.fm/s/1173f6c14/podcast/rss"
 },
 {
  "title": "השבוע - פודקאסט הארץ",
  "feed": "https://www.omnycontent.com/d/playlist/397b9456-4f75-4509-acff-ac0600b4a6a4/fdef9415-eb17-45d7-85fd-ac08009235b2/4c9f1a8f-8a0e-4f10-b01f-ac08009235b7/podcast.rss"
 },
 {
  "title": "למי אכפת",
  "feed": "https://feeds.megaphone.fm/POLTD2511095846"
 },
 {
  "title": "הפודיום",
  "feed": "https://feeds.megaphone.fm/POLTD9711993371"
 },
 {
  "title": "בזמן שעבדתם",
  "feed": "https://www.omnycontent.com/d/playlist/2ee97a4e-8795-4260-9648-accf00a38c6a/a5d4b51f-5b9e-43db-84da-ace100c04108/0ab18f83-1327-4f4e-9d7a-ace100c0411f/podcast.rss"
 },
 {
  "title": "הקרנף עם יואב רבינוביץ",
  "feed": "https://feeds.megaphone.fm/POLTD2316968013"
 },
 {
  "title": "תרגעו",
  "feed": "https://feeds.megaphone.fm/POLTD1402661471"
 },
 {
  "title": "הסכתוס",
  "feed": "https://www.haaretz.co.il/srv/podcast-channel?id=0000018f-7c4b-d430-a38f-fdefbcbe0001&caller=apple"
 },
 {
  "title": "בוקר חדש",
  "feed": "https://rss.buzzsprout.com/2186489.rss"
 },
 {
  "title": "בגג של יצחקי",
  "feed": "https://feeds.transistor.fm/7491e803-4380-4b16-af05-0e30d031de2e"
 },
 {
  "title": "קיקטוק",
  "feed": "https://anchor.fm/s/10ea649d8/podcast/rss"
 },
 {
  "title": "ציון 3",
  "feed": "http://tziun3.co.il/?feed=podcast"
 },
 {
  "title": "פודקאסט רצח",
  "feed": "https://anchor.fm/s/96515a90/podcast/rss"
 },
 {
  "title": "הפודקאסט של נדב פרי",
  "feed": "https://anchor.fm/s/10ea64dc0/podcast/rss"
 },
 {
  "title": "מנועי הכסף",
  "feed": "https://www.omnycontent.com/d/playlist/178d72a7-a889-4132-8008-a5cc014ed109/c39a4cf6-7e84-43fa-bfa4-b31b00e05cfc/8a5aa674-a749-43c7-86c3-b31b00e06274/podcast.rss"
 },
 {
  "title": "התשובה עם דורון פישלר",
  "feed": "https://www.spreaker.com/show/4228834/episodes/feed"
 },
 {
  "title": "חוץ לארץ",
  "feed": "https://www.omnycontent.com/d/playlist/397b9456-4f75-4509-acff-ac0600b4a6a4/6b5c19f7-a385-49c0-bb95-ad4a0071daea/08535d76-8bf4-4bf2-af8d-ad4a007205a3/podcast.rss"
 },
 {
  "title": "לשחרר את הדב",
  "feed": "https://feeds.megaphone.fm/POLTD4092016598"
 },
 {
  "title": "הברזייה",
  "feed": "https://www.omnycontent.com/d/playlist/de0f04c1-f777-4661-b029-af6d01426cad/73069524-c0c4-4ec1-b655-af7800691ad1/b5976e80-88f6-48df-9588-af7800691afb/podcast.rss"
 },
 {
  "title": "גיקונומי",
  "feed": "https://feed.podbean.com/geekonomy/feed.xml"
 },
 {
  "title": "שוט",
  "feed": "https://feeds.megaphone.fm/POLTD5343938075"
 },
 {
  "title": "מפלגת המחשבות",
  "feed": "https://rss.buzzsprout.com/1740993.rss"
 },
 {
  "title": "איך לעשות דברים",
  "feed": "https://www.omnycontent.com/d/playlist/23f697a0-7e6a-4e96-a223-a82c00962b12/3517d295-90e8-402c-8427-b1d7009673de/2d82c4ea-e957-40ac-b9f9-b1d7009a3e97/podcast.rss"
 },
 {
  "title": "החיים החדשים של רומי גונן",
  "feed": "https://www.omnycontent.com/d/playlist/2ee97a4e-8795-4260-9648-accf00a38c6a/e0e7792b-8eaf-4f49-9f94-b42700b6b6fd/99a84147-703b-4c06-8b0d-b42700b6bb36/podcast.rss"
 },
 {
  "title": "האינטרסנטים",
  "feed": "https://www.omnycontent.com/d/playlist/397b9456-4f75-4509-acff-ac0600b4a6a4/161f6359-650a-4e72-9090-ac07017cc8e0/f4c15dc2-8e2b-4391-b534-ac07017cc8f8/podcast.rss"
 },
 {
  "title": "מיכה סטוקס על שוק ההון",
  "feed": "https://app.kajabi.com/podcasts/2147619382/feed"
 },
 {
  "title": "חושבים טוב",
  "feed": "https://feeds.simplecast.com/w2pVTj5d"
 },
 {
  "title": "השקעות לעצלנים",
  "feed": "https://anchor.fm/s/ef1f5500/podcast/rss"
 }
]

POD_PROMPTS = {
    'pod_menu1': "להאזנה, הקישו את מספר הפודקאסט וסולמית. 1, אחד ביום . 2, פודקאסט שולחן 4 . 3, לוינסון על הבוקר . 4, השבוע - פודקאסט הארץ . 5, למי אכפת . 6, הפודיום . 7, בזמן שעבדתם . 8, הקרנף עם יואב רבינוביץ . 9, תרגעו . 10, הסכתוס. לרשימה הבאה, הקישו 0 וסולמית.",
    'pod_menu2': "להאזנה, הקישו את מספר הפודקאסט וסולמית. 11, בוקר חדש . 12, בגג של יצחקי . 13, קיקטוק . 14, ציון 3 . 15, פודקאסט רצח . 16, הפודקאסט של נדב פרי . 17, מנועי הכסף . 18, התשובה עם דורון פישלר . 19, חוץ לארץ . 20, לשחרר את הדב. לרשימה הבאה, הקישו 0 וסולמית.",
    'pod_menu3': "להאזנה, הקישו את מספר הפודקאסט וסולמית. 21, הברזייה . 22, גיקונומי . 23, שוט . 24, מפלגת המחשבות . 25, איך לעשות דברים . 26, החיים החדשים של רומי גונן . 27, האינטרסנטים . 28, מיכה סטוקס על שוק ההון . 29, חושבים טוב . 30, השקעות לעצלנים. לרשימה הבאה, הקישו 0 וסולמית.",
    'pod_searching': 'רגע אחד, אני מביאה את הפרק האחרון. אם הפרק ארוך, זה יכול לקחת דקה-שתיים.',
    'pod_wait': 'עוד קצת, הפרק כבר כמעט כאן.',
    'pod_notfound': 'סליחה, לא הצלחתי להביא את הפרק. נסו פודקאסט אחר.',
    'pod_after': 'לפרק קודם, הקישו 1. לפרק הבא, הקישו 2. לתפריט הפודקאסטים, הקישו 3. לתפריט הראשי, הקישו 4.',
}
pod_jobs = {}

def feed_enclosures(feed_url):
    req = urllib.request.Request(feed_url, headers={'User-Agent': 'Mozilla/5.0'})
    data = urllib.request.urlopen(req, timeout=25).read().decode('utf-8', 'ignore')
    encs = re.findall(r'<enclosure[^>]*url="([^"]+)"', data)
    return encs

def fetch_pod(call_id, pod_idx, ep_idx):
    job = pod_jobs[call_id]
    tmp = f'/tmp/pod-{call_id}'
    try:
        import imageio_ffmpeg
        pod = PODCASTS[pod_idx]
        encs = feed_enclosures(pod['feed'])
        if ep_idx >= len(encs):
            ep_idx = len(encs) - 1
        if ep_idx < 0:
            ep_idx = 0
        url = encs[ep_idx]
        log.info('pod %s ep %d: %s', pod['title'], ep_idx, url[:80])
        mp3 = tmp + '.src'
        urllib.request.urlretrieve(url, mp3)
        out = tmp + '.wav'
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-y', '-i', mp3,
                        '-ar', '16000', '-ac', '1', '-f', 'wav', out],
                       check=True, capture_output=True, timeout=600)
        name = 'pod' + re.sub(r'\D', '', call_id)[-6:]
        with open(out, 'rb') as f:
            ym_upload(f.read(), name + '.wav', f'/3/{name}.wav')
        for f_ in (tmp + '.src', out):
            try: os.remove(f_)
            except OSError: pass
        job.update(status='ready', name=name, ep=ep_idx)
        log.info('pod ready call=%s %s ep %d', call_id, pod['title'], ep_idx)
    except Exception as e:
        log.warning('pod fetch failed call=%s: %s', call_id, e)
        job.update(status='error', err=str(e)[:200])


def pod_display_name(title):
    t = (title or '').split('|')[0].strip()
    t = re.sub(r'[A-Za-z][A-Za-z0-9&\' .]*$', '', t).strip(' -–:')
    if len(re.findall(r'[\u0590-\u05FF]', t)) < 3:
        t = (title or '').strip()
    return t[:60]

def refresh_podcasts():
    """Weekly: pull Apple Podcasts IL chart, verify feeds, rebuild list + Hila menu prompts."""
    global PODCASTS
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(
            'https://rss.marketingtools.apple.com/api/v2/il/podcasts/top/50/podcasts.json',
            headers={'User-Agent': 'Mozilla/5.0'}), timeout=25).read())
        results = d['feed']['results']
        ids = ','.join(r['id'] for r in results)
        lk = json.loads(urllib.request.urlopen(f'https://itunes.apple.com/lookup?id={ids}&entity=podcast', timeout=25).read())
        feeds = {str(r.get('collectionId')): r.get('feedUrl') for r in lk.get('results', [])}
        hebrew = lambda s: bool(re.search(r'[\u0590-\u05FF]', s or ''))
        new = []
        for r in results:
            feed = feeds.get(r['id'])
            if not feed or not hebrew(r.get('name', '')):
                continue
            try:
                data = urllib.request.urlopen(urllib.request.Request(feed, headers={'User-Agent': 'Mozilla/5.0'}), timeout=15).read(4_000_000).decode('utf-8', 'ignore')
                if not re.search(r'<enclosure[^>]*url="', data):
                    continue
            except Exception:
                continue
            new.append({'title': pod_display_name(r['name']), 'feed': feed})
            if len(new) >= 30:
                break
        if len(new) < 20:
            log.warning('podcast refresh: only %d valid feeds, keeping old list', len(new))
            return
        PODCASTS = new
        for pg in range(3):
            lines = [f'{pg*10+i+1}, {e["title"]}' for i, e in enumerate(new[pg*10:pg*10+10]) if e]
            if not lines:
                continue
            txt = 'להאזנה, הקישו את מספר הפודקאסט וסולמית. ' + ' . '.join(lines) + '. לרשימה הבאה, הקישו 0 וסולמית.'
            ym_upload(tts_wav(txt), f'pod_menu{pg+1}.wav', f'/3/pod_menu{pg+1}.wav')
        log.info('podcast refresh: list updated (%d podcasts) + menus regenerated', len(new))
    except Exception as e:
        log.warning('podcast refresh failed: %s', e)

def podcast_refresh_loop():
    time.sleep(3 * 24 * 3600)
    while True:
        refresh_podcasts()
        time.sleep(7 * 24 * 3600)

threading.Thread(target=podcast_refresh_loop, daemon=True).start()


@app.route('/yemot-pod', methods=['GET', 'POST'])
def yemot_pod():
    params = request.values
    if params.get('secret') != BRIDGE_SECRET:
        return 'forbidden', 403
    call_id = params.get('ApiCallId') or str(time.time_ns())
    if params.get('hangup') == 'yes':
        with lock:
            pod_jobs.pop(call_id, None)
        return text_response('')

    s_val, turn = None, 0
    for k, v in params.items():
        if re.fullmatch(r'S\d+', k):
            s_val, turn = v, int(k[1:])

    with lock:
        job = pod_jobs.setdefault(call_id, {'stage': 'menu', 'page': 1, 'status': 'idle', 'started': time.time()})

    if s_val is None:
        return text_response('read=f-pod_menu1=S1,,1,2,Digits,yes')

    try:
        stage = job['stage']

        if stage == 'menu':
            v = (s_val or '').strip()
            if v == '0' or v == '':
                job['page'] = job.get('page', 1) % 3 + 1
                return text_response(f"read=f-pod_menu{job['page']}=S{turn+1},,1,2,Digits,yes")
            if v.isdigit() and 1 <= int(v) <= len(PODCASTS):
                job.update(stage='pod_wait', status='working', idx=int(v) - 1, ep=0, started=time.time())
                threading.Thread(target=fetch_pod, args=(call_id, job['idx'], 0), daemon=True).start()
                return text_response(f'read=f-pod_searching=S{turn+1},no,no')
            return text_response(f"read=f-pod_menu{job.get('page',1)}=S{turn+1},,1,2,Digits,yes")

        if stage == 'pod_wait':
            st = job.get('status')
            if st == 'working':
                if time.time() - job.get('started', 0) > 300:
                    job.update(stage='menu', status='idle', page=1)
                    return text_response(f'read=f-pod_notfound.f-pod_menu1=S{turn+1},,1,2,Digits,yes')
                return text_response(f'read=f-pod_wait=S{turn+1},no,no')
            if st == 'error':
                job.update(stage='menu', status='idle', page=1)
                return text_response(f'read=f-pod_notfound.f-pod_menu1=S{turn+1},,1,2,Digits,yes')
            job['stage'] = 'pod_play'
            return text_response(f"read=f-{job['name']}=S{turn+1},no,no")

        if stage == 'pod_play':
            job['stage'] = 'pod_after'
            return text_response(f'read=f-pod_after=S{turn+1},,1,1,Digits,yes')

        if stage == 'pod_after':
            v = (s_val or '').strip()
            if v == '4':
                with lock:
                    pod_jobs.pop(call_id, None)
                return text_response('go_to_folder=/')
            if v == '3':
                job.update(stage='menu', page=1)
                return text_response(f'read=f-pod_menu1=S{turn+1},,1,2,Digits,yes')
            if v in ('1', '2'):
                ep = job.get('ep', 0) + (1 if v == '1' else -1)
                if ep < 0:
                    ep = 0
                job.update(stage='pod_wait', status='working', started=time.time())
                threading.Thread(target=fetch_pod, args=(call_id, job['idx'], ep), daemon=True).start()
                return text_response(f'read=f-pod_searching=S{turn+1},no,no')
            job['stage'] = 'pod_after'
            return text_response(f'read=f-pod_after=S{turn+1},,1,1,Digits,yes')

        job['stage'] = 'menu'
        return text_response(f'read=f-pod_menu1=S{turn+1},,1,2,Digits,yes')

    except Exception as e:
        log.exception('pod call=%s error: %s', call_id, e)
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
        search_title = None
        if m:
            video_id = m.group(1)
        else:
            video_id, search_title = yt_search_video_id(q)
        t1 = time.time()
        title, dur = yt_download(video_id, tmp + '.%(ext)s')
        if title == 'שיר' and search_title:
            title = search_title
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
