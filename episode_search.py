"""Opt-in metadata episode discovery. No audio is fetched until explicit selection."""
import re, time, threading, hashlib, html, unicodedata, ipaddress
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, urljoin
from collections import deque, OrderedDict


def norm(text):
    text=unicodedata.normalize('NFKD',html.unescape(re.sub('<[^>]+>',' ',str(text or ''))))
    return ' '.join(''.join(c for c in text if not unicodedata.combining(c)).lower().split())


def public_url(url):
    p=urlparse(str(url or ''))
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:return False
    host=p.hostname.lower()
    if host=='localhost' or host.endswith(('.localhost','.local')):return False
    try:return ipaddress.ip_address(host).is_global
    except ValueError:return '.' in host


def install(ns):
    from flask import request
    app=ns['app']; original=app.view_functions['yemot_pod']; cache=OrderedDict(); requests_at=deque(); guard=threading.Lock()
    def apple(term,entity,limit):
        key=(norm(term),entity)
        with guard:
            now=time.monotonic()
            if key in cache and now-cache[key][0]<300:return cache[key][1]
            while requests_at and now-requests_at[0]>60:requests_at.popleft()
            if len(requests_at)>=15:raise RuntimeError('metadata search busy')
            requests_at.append(now)
        r=ns['requests'].get('https://itunes.apple.com/search',params={'term':term,'entity':entity,'media':'podcast','country':'IL','limit':limit},timeout=(5,15));r.raise_for_status()
        rows=r.json().get('results',[])
        with guard:
            cache[key]=(time.monotonic(),rows);cache.move_to_end(key)
            while len(cache)>64:cache.popitem(last=False)
        return rows
    def feed(url):
        if not public_url(url):raise ValueError('invalid feed URL')
        buf=bytearray();deadline=time.monotonic()+25
        for _ in range(6):
            if not public_url(url):raise ValueError('invalid feed URL')
            with ns['requests'].get(url,headers=ns['POD_HEADERS'],stream=True,allow_redirects=False,timeout=(5,20)) as r:
                if r.status_code in (301,302,303,307,308):
                    url=urljoin(url,r.headers.get('Location',''));continue
                r.raise_for_status()
                for chunk in r.iter_content(65536):
                    if time.monotonic()>deadline:raise ValueError('feed search timed out')
                    buf.extend(chunk)
                    if len(buf)>12*1048576:raise ValueError('feed too large for search')
                break
        else:raise ValueError('too many feed redirects')
        ET.fromstring(bytes(buf))  # Do not claim complete search on a truncated/malformed feed.
        rows=ns['episode_items'](bytes(buf))
        return [x for x in rows if public_url(x['url'])]
    def match(rows,query):
        words=norm(query).split()
        found=[]
        for row in rows:
            title=norm(row['title']);combined=title+' '+norm(row.get('description',''))
            if words and all(w in combined for w in words):found.append((sum(w in title for w in words),row))
        return [r for _,r in sorted(found,key=lambda x:-x[0])]
    def raw(body):return ns['text_response'](body)
    def input_prompt(name,turn,optional=False):
        # Preserve normal multitap * separators and # submission. Only optional show permits zero digits.
        fields=[f'S{turn+1}','no','120','0' if optional else '1','20','No','no','no','','','','Ok' if optional else '','','','no']
        return raw('read='+{'ps_query':'f-ps_query.f-pod_typehow','ps_show':'f-ps_show.f-pod_typehow','ps_retry':'f-ps_none.f-ps_query.f-pod_typehow','ps_retry_show':'f-ps_none.f-ps_show.f-pod_typehow'}.get(name,'f-'+name)+'='+','.join(fields))
    def value(p):
        turns=[(int(re.search(r'\d+$',k).group()),v)for k,v in p.items()if re.fullmatch(r'(?:S|EP)\d+',k)]
        return max(turns,default=(0,None))
    def decode(v):
        return ns['multitap_decode'](v,'he') if v and re.fullmatch('[0-9*]+',v) else ''
    def valid(call,job,token):return ns['pod_jobs'].get(call)is job and job.get('search_token')==token
    def launch(call,job,kind,query=None,selected=None):
        token=hashlib.sha256((str(time.time_ns())+call).encode()).hexdigest()[:16]
        job.update(search_started=time.monotonic(),stage='search_loading',search_token=token,search_kind=kind,search_status='working',search_page=0)
        def work():
            try:
                if kind=='shows':
                    rows=[{'title':x.get('collectionName')or x.get('trackName')or'', 'feed':x['feedUrl']}for x in apple(query,'podcast',8)if public_url(x.get('feedUrl'))][:7]
                    result={'search_rows':rows,'search_next_stage':'search_show_pick'}
                elif kind=='episodes':
                    show=job.get('search_show')
                    if show:
                        episodes=feed(show['feed']); rows=match(episodes,query)
                        result={'search_feed_rows':episodes,'search_rows':rows,'search_next_stage':'search_episode_pick'}
                    else:
                        seen=set();rows=[]
                        for x in apple(query,'podcastEpisode',30):
                            ident=x.get('episodeGuid')or x.get('episodeUrl');url=x.get('episodeUrl');f=x.get('feedUrl')
                            if x.get('kind')!='podcast-episode' or not ident or ident in seen or not public_url(url)or not public_url(f):continue
                            seen.add(ident);rows.append({'id':ident,'title':x.get('trackName')or'פרק ללא שם','published':x.get('releaseDate'),'url':url,'show':{'title':x.get('collectionName')or'פודקאסט','feed':f}})
                        result={'search_rows':rows,'search_next_stage':'search_episode_pick'}
                elif kind=='verify':
                    show=selected['show'];episodes=feed(show['feed'])
                    exact=next((e for e in episodes if e['id']==selected['id'] or e['url']==selected['url']),None)
                    if exact is None:raise ValueError('episode not verified in current publisher feed')
                    result={'search_verified':exact,'search_feed_rows':episodes,'search_show':show,'search_next_stage':'search_verified'}
                if valid(call,job,token):job.update(**result,search_status='ready')
            except Exception as e:
                ns['log'].warning('episode search failed: %s',str(e)[:140])
                if valid(call,job,token):job['search_status']='error'
        ns['threading'].Thread(target=work,daemon=True).start()
    def menu(job,turn):
        rows=job.get('search_rows',[]);pg=job.get('search_page',0);chunk=rows[pg*7:(pg+1)*7]
        pieces=['בחרו מספר וסולמית. לחיפוש חדש אפס וסולמית. לחזרה שמונה וסולמית. לעוד תוצאות תשע וסולמית. לראשי כוכבית']
        for i,r in enumerate(chunk,1):
            label=r['title']
            if r.get('show'):label+=' מתוך '+r['show']['title']
            if r.get('published'):label+=' '+r['published'][:10]
            pieces.append(re.sub(r'[&=\r\n]',' ',label)[:240]+f', {i}')
        text='. '.join(pieces);name='psr_'+hashlib.sha256(text.encode()).hexdigest()[:20]
        if job.get('search_prompt')!=name:
            job.update(search_prompt=name,search_prompt_ready=False,search_prompt_started=time.monotonic())
            def prepare():
                try:
                    ns['ym_upload'](ns['tts_wav'](text),name+'.wav','/3/'+name+'.wav')
                    if job.get('search_prompt')==name:job['search_prompt_ready']=True
                except Exception:
                    if job.get('search_prompt')==name:job['search_prompt_error']=True
            ns['threading'].Thread(target=prepare,daemon=True).start()
        if job.pop('search_prompt_error',False) or (not job.get('search_prompt_ready') and time.monotonic()-job.get('search_prompt_started',0)>60):
            job['stage']='search_query';return input_prompt('ps_retry',turn)
        chain='f-'+name if job.get('search_prompt_ready')else 'f-pod_wait'
        # EP stays numeric input under outer navigation: # submits, * returns root.
        return raw(f'read={chain}=EP{turn+1},no,1,0,10,No,no,no,,,1,Ok,,,no')
    def view():
        p=request.values
        if p.get('secret')!=ns['BRIDGE_SECRET']:return original()
        call=p.get('ApiCallId');job=ns['pod_jobs'].get(call);turn,v=value(p)
        if p.get('hangup')=='yes':return original()
        if job and str(job.get('stage','')).startswith('search_'):
            stage=job['stage']
            if stage in ('search_show_pick','search_episode_pick') and v=='*':
                ns['pod_jobs'].pop(call,None);return raw('go_to_folder=/')
            if stage=='search_optional_show':
                if v in ('','#'):
                    job.update(stage='search_query',search_show=None);return input_prompt('ps_query',turn)
                q=decode(v)
                if not q:return input_prompt('ps_show',turn,True)
                launch(call,job,'shows',q);return raw(ns['play_chain']('f-pod_wait',f'S{turn+1}'))
            if stage=='search_query':
                q=decode(v)
                if not q:return input_prompt('ps_query',turn)
                launch(call,job,'episodes',q);return raw(ns['play_chain']('f-pod_wait',f'S{turn+1}'))
            if stage=='search_loading':
                state=job.get('search_status')
                if state=='working' and time.monotonic()-job.get('search_started',0)>60:state='error'
                if state=='working':return raw(ns['play_chain']('f-pod_wait',f'S{turn+1}'))
                if state=='error':
                    job['stage']='search_query';return input_prompt('ps_retry',turn)
                stage=job['stage']=job['search_next_stage']
                if stage=='search_verified':
                    job.update(custom=job['search_show'],idx=0,episodes=job['search_feed_rows'],season_selected=False)
                    return ns['start_episode'](call,job,job['search_verified'],turn)
                if not job.get('search_rows'):
                    job['stage']='search_optional_show'if stage=='search_show_pick'else'search_query'
                    return input_prompt('ps_retry_show'if stage=='search_show_pick'else'ps_retry',turn,stage=='search_show_pick')
                return menu(job,turn)
            if stage in ('search_show_pick','search_episode_pick'):
                if v in (None,'','None'):return menu(job,turn)
                if v=='0':
                    job['stage']='search_optional_show' if stage=='search_show_pick' else 'search_query'
                    return input_prompt('ps_show' if stage=='search_show_pick' else 'ps_query',turn,stage=='search_show_pick')
                if v=='8':
                    job.update(stage='search_optional_show',search_show=None);return input_prompt('ps_show',turn,True)
                if v=='9':
                    pages=max(1,(len(job.get('search_rows',[]))+6)//7);job['search_page']=(job.get('search_page',0)+1)%pages
                    return menu(job,turn)
                if v.isdigit() and 1<=int(v)<=7:
                    idx=job.get('search_page',0)*7+int(v)-1;rows=job.get('search_rows',[])
                    if idx<len(rows):
                        selected=rows[idx]
                        if stage=='search_show_pick':
                            job.update(stage='search_query',search_show=selected);return input_prompt('ps_query',turn)
                        if job.get('search_show'):
                            job.update(custom=job['search_show'],idx=0,episodes=job['search_feed_rows'],season_selected=False)
                            return ns['start_episode'](call,job,selected,turn)
                        launch(call,job,'verify',selected=selected);return raw(ns['play_chain']('f-pod_wait',f'S{turn+1}'))
                return menu(job,turn)
        if job and job.get('stage')=='entry' and v=='4':
            job.update(stage='search_optional_show',search_show=None);return input_prompt('ps_show',turn,True)
        result=original()
        body=result.get_data(as_text=True)if hasattr(result,'get_data')else''
        # Append to the approved existing recording, do not replace it or any existing option.
        if 'read=f-pod_entry=' in body:result=raw(body.replace('read=f-pod_entry=','read=f-pod_entry.f-ps_entry='))
        return result
    ns['_episode_search_test']={'apple':apple,'feed':feed,'match':match,'view':view}
    app.view_functions['yemot_pod']=view
