"""Link-only catalog and opt-in IVR adapter. No audio downloaded here."""
import json, re, os
import urllib.request
from urllib.parse import urlparse, parse_qs
from flask import request, jsonify

def install(ns):
    app = ns['app']
    enabled = os.environ.get('ENABLE_ADMIN_SONG_LINKS', '0') == '1'
    def finish(call):
        with ns['lock']: job=ns['song_jobs'].pop(call,None)
        if job:
            def clean():
                for name in job.get('link_files',set()):
                    try: ns['ym_delete'](ns['SONG_DIR']+'/'+name+'.wav')
                    except Exception: ns['log'].warning('link-list temporary file cleanup failed')
            ns['threading'].Thread(target=clean,daemon=True).start()

    def vid(url):
        u = urlparse(url)
        if u.scheme not in ('http', 'https'): raise ValueError('invalid URL')
        host = (u.hostname or '').lower()
        if host == 'youtu.be': v = u.path.strip('/').split('/')[0]
        elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
            v = parse_qs(u.query).get('v', [''])[0]
            if u.path.startswith(('/shorts/', '/live/')): v = u.path.split('/')[2]
        else: raise ValueError('YouTube links only')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', v): raise ValueError('invalid video')
        return v

    @app.route('/admin-song-links', methods=['GET', 'POST'])
    def admin_song_links():
        if not ns['admin_secret_ok'](): return 'forbidden', 403
        if request.method == 'GET': return jsonify(ok=True, enabled=enabled)
        if not enabled: return jsonify(ok=False, error='חיבור הרשימות עדיין לא הופעל'), 503
        try:
            j = request.get_json(force=True)
            query = str(j.get('query', '')).strip()[:300]
            if not query: raise ValueError('הזן חיפוש או קישור')
            mode=j.get('mode','search')
            if mode == 'playlists':
                ns['_yt_cfg']()
                body={'context': ns['_yt_tv_context'](), 'query':query,'params':'EgIQAw=='}
                data=json.load(urllib.request.urlopen(urllib.request.Request(
                    'https://www.youtube.com/youtubei/v1/search?prettyPrint=false&key='+ns['_YT']['key'],
                    data=json.dumps(body).encode(),headers=ns['_yt_headers']()),timeout=30))
                playlists=[];seen=set()
                def scan(x):
                    if isinstance(x,dict):
                        r=x.get('playlistRenderer')
                        if r and r.get('playlistId') not in seen:
                            seen.add(r['playlistId']);playlists.append({'title':ns['_yt_result_title'](r.get('title')) or 'פלייליסט', 'url':'https://www.youtube.com/playlist?list='+r['playlistId']})
                        lv=x.get('lockupViewModel')
                        if lv and lv.get('contentId') and not re.fullmatch(r'[A-Za-z0-9_-]{11}',lv['contentId']):
                            ident=lv['contentId']
                            if ident.startswith(('PL','OL','RD','UU')) and ident not in seen:
                                seen.add(ident);title=ns['_yt_result_title'](((lv.get('metadata') or {}).get('lockupMetadataViewModel') or {}).get('title'))
                                playlists.append({'title':title or 'פלייליסט','url':'https://www.youtube.com/playlist?list='+ident})
                        for v in x.values():scan(v)
                    elif isinstance(x,list):
                        for v in x:scan(v)
                scan(data.get('contents',{}))
                return jsonify(ok=True,playlists=playlists[:20])
            if mode == 'artist':
                rows=ns['yt_search_results'](query,limit=200)
            elif mode == 'import':
                u = urlparse(query)
                if u.scheme != 'https' or u.hostname not in ('youtube.com','www.youtube.com','m.youtube.com','music.youtube.com'): raise ValueError('נדרש קישור לערוץ או לפלייליסט יוטיוב')
                if u.path not in ('/playlist', '/watch') and not u.path.startswith(('/@', '/channel/', '/c/', '/user/')): raise ValueError('קישור ערוץ או פלייליסט בלבד')
                if u.path.startswith(('/@', '/channel/', '/c/', '/user/')) and not u.path.rstrip('/').endswith(('/videos','/shorts','/streams')):
                    query = query.rstrip('/') + '/videos'
                import yt_dlp
                with yt_dlp.YoutubeDL({'quiet':True,'skip_download':True,'extract_flat':True,'playlistend':200,'socket_timeout':15,'ignoreerrors':True,'retries':1}) as y:
                    info = y.extract_info(query, download=False)
                rows = []
                def walk(info):
                    if not isinstance(info,dict): return
                    if 'entries' in info:
                        for entry in info.get('entries') or []:
                            if len(rows)>=200: break
                            walk(entry)
                    elif re.fullmatch(r'[A-Za-z0-9_-]{11}', str(info.get('id',''))): rows.append((info['id'],info.get('title') or info['id']))
                walk(info)
            elif query.startswith(('https://','http://')): rows=[(vid(query),'')]
            else: rows=ns['yt_search_results'](query, limit=15)
            songs=[];seen=set()
            for v,title in rows:
                if re.fullmatch(r'[A-Za-z0-9_-]{11}',str(v)) and v not in seen:
                    seen.add(v);songs.append({'url':'https://www.youtube.com/watch?v='+v,'title':title or v})
            return jsonify(ok=True,songs=songs)
        except Exception as e: return jsonify(ok=False,error=str(e)[:180]),502

    @app.route('/yemot-link-list', methods=['GET','POST'])
    def yemot_link_list():
        p=request.values
        if not ns['BRIDGE_SECRET'] or p.get('secret') != ns['BRIDGE_SECRET']: return 'forbidden',403
        if not enabled: return ns['text_response']('go_to_folder=/5')
        # Yemot's API directory remains /2 so its playback prompts are shared with the existing player.
        config=p.get('config','')
        if not re.fullmatch(r'/5/_songs_[a-f0-9]{32}_[0-9]\.ini',config): return 'bad config',400
        call=p.get('ApiCallId')
        if not call or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',call): return 'bad call',400
        if p.get('hangup')=='yes':
            finish(call)
            return ns['text_response']('')
        with ns['lock']:
            job=ns['song_jobs'].get(call)
        if job is None:
            try:
                manifest=json.loads(ns['ym_download'](config).decode('utf-8'))
                songs=manifest['songs']
                if not isinstance(songs,list) or not 1<=len(songs)<=200: raise ValueError('empty list')
                queue=[(vid(s['url']),str(s.get('title',''))[:180]) for s in songs]
                job={'stage':'wait','status':'idle','mode':'artist','queue':queue,'qidx':0,'started':ns['time'].time(), 'link_list':True}
                if p.get('ApiPhone'):job['phone']=p['ApiPhone']
                with ns['lock']: ns['song_jobs'][call]=job
                ns['start_prefetch'](call,0)
                return ns['wait_step'](call,job,0)
            except Exception:
                return ns['text_response']('go_to_folder=/5')
        if job.get('stage')=='ask':
            finish(call)
            return ns['text_response']('go_to_folder=/5')
        # Never enter the old audio-copy playlist-saving or radio expansion flow from a link list.
        turns=[(int(k[1:]),v) for k,v in p.items() if re.fullmatch(r'S\d+',k)]
        if turns and job.get('stage')=='after':
            turn,value=max(turns)
            if value=='1': return ns['text_response'](f'read=f-song_auto_next=S{turn+1},no,1,1,7,No,yes,,,,,,,,no')
            if value=='3':
                finish(call)
                return ns['text_response']('go_to_folder=/5')
            if job.get('qidx',0)+1>=len(job['queue']):
                finish(call)
                return ns['text_response']('go_to_folder=/5')
        return ns['yemot_song']()
