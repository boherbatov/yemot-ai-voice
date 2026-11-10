"""Call-scoped work guards. Does not change search order or deletion policy."""
import contextvars,functools,hashlib,threading
CURRENT=contextvars.ContextVar('regular_call_work',default=None)
_LOCK=threading.RLock()
_ACTIVE={}
class CallEnded(RuntimeError):pass
def owner():
 scope=CURRENT.get()
 return scope['call'] if scope else None
def check():
 scope=CURRENT.get()
 if not scope:return
 with _LOCK:
  if _ACTIVE.get(scope['call']) is not scope['generation']:raise CallEnded('call left or changed feature')
 job=scope['store'].get(scope['call'])
 if not scope.get('captured') and job is not None:scope.update(job=job,captured=True)
 if scope.get('captured') and job is not scope['job']:raise CallEnded('call left or job replaced')
def media_id(call):return hashlib.sha256(str(call).encode()).hexdigest()[:24]
def install(ns):
 real=ns['threading'];app=ns['app']
 class ScopedThread(real.Thread):
  def __init__(self,*args,**kwargs):
   self._scope_context=contextvars.copy_context();super().__init__(*args,**kwargs)
  def run(self):
   def work():
    try:check();super(ScopedThread,self).run()
    except CallEnded:ns['log'].info('stopped work for ended call')
   return self._scope_context.run(work)
 class ThreadingProxy:
  Thread=ScopedThread
  def __getattr__(self,name):return getattr(real,name)
 ns['threading']=ThreadingProxy()
 for name in ('ym_upload','ym_upload_text','ym_download','ym_get','tts_wav','_tts_wav_once','_download_convert','_run_ff','_probe_duration','hngn_fetch_search','yt_search_results','yt_player','yt_related','yt_download','yt_stream_info','_yt_cfg','_yt_token','_jm_get','jamendo_download','_gd_api','_gd_token','drive_download','_ai_clean_query','groq_stt','groq_chat','gemini_chat','web_context','hub_sources','_gnews_items','news_headlines','itunes_podcast_search_multi','itunes_podcast_search','feed_enclosures','_pod_size','_pod_stream_parts','wiki_article_by_title','wiki_search_titles','wiki_article_text','fetch_episode_menu'):
  original=ns.get(name)
  if not original:continue
  def wrap(fn):
   @functools.wraps(fn)
   def guarded(*args,**kwargs):
    check();result=fn(*args,**kwargs);check();return result
   return guarded
  ns[name]=wrap(original)
 stores={'yemot':'sessions','yemot_song':'song_jobs','yemot_pod':'pod_jobs','yemot_wiki':'wiki_jobs','yemot_link_list':'song_jobs'}
 for endpoint,store_name in stores.items():
  original=app.view_functions.get(endpoint)
  if not original:continue
  def wrap_view(fn,store,feature):
   @functools.wraps(fn)
   def view():
    from flask import request
    call=request.values.get('ApiCallId')
    if not call or request.values.get('secret')!=ns['BRIDGE_SECRET']:return fn()
    if request.values.get('hangup')=='yes':
     with _LOCK:_ACTIVE.pop(call,None)
     # Keep existing hangup cleanup and history behavior outside canceled scope.
     cleanup_token=CURRENT.set(None)
     try:return fn()
     finally:CURRENT.reset(cleanup_token)
    with _LOCK:
     generation=_ACTIVE.get(call)
     if generation is None or generation['feature']!=feature:
      generation={'feature':feature};_ACTIVE[call]=generation
    job=store.get(call)
    scope={'call':call,'store':store,'job':job,'captured':job is not None,'generation':generation};token=CURRENT.set(scope)
    try:
     result=fn()
     body=result.get_data(as_text=True) if hasattr(result,'get_data') else ''
     if 'go_to_folder=/' in body and body.split('go_to_folder=/',1)[1].split('&',1)[0].split('\n',1)[0]=='':
      with _LOCK:
       if _ACTIVE.get(call) is generation:_ACTIVE.pop(call,None)
     return result
    except CallEnded:return ns['text_response']('')
    finally:CURRENT.reset(token)
   return view
  app.view_functions[endpoint]=wrap_view(original,ns[store_name],store_name)
