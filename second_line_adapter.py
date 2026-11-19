"""LOCAL PROTOTYPE ONLY. Shared old process; no global credential replacement."""
import contextvars, hmac, re
from urllib.parse import unquote
from flask import request
_NEW = contextvars.ContextVar('second_pbx_song_only', default=False)

def install(ns, *, system, password, secret):
    app=ns['app']; old_requests=ns['requests']; token=system+':'+password
    original_song=app.view_functions['yemot_song']; original_response=ns['text_response']
    def mapped(path):
        raw=str(path)
        decoded=unquote(raw)
        if any(x in decoded.split('/')for x in ('.','..')) or '\x00' in decoded or unquote(decoded)!=decoded:
            raise ValueError('ambiguous PBX path')
        p=raw; marker='ivr2:' if p.startswith('ivr2:') else '';p=p[len(marker):]
        for old,new in [('/2','/9/2'),('/AI','/9/AI')]:
            if p==old or p.startswith(old+'/'):return marker+new+p[len(old):]
        if any(p==root or p.startswith(root+'/')for root in ['/9/2','/9/AI','/16']):return marker+p
        raise ValueError('new-line song callback attempted unrelated PBX path')
    class ScopedRequests:
        def __getattr__(self,name):return getattr(old_requests,name)
        def _call(self,method,url,*args,**kw):
            if _NEW.get() and url.startswith(ns['YM_API']+'/'):
                kw=dict(kw)
                for part in ['params','data']:
                    if isinstance(kw.get(part),dict):
                        values=dict(kw[part])
                        if 'token' in values:values['token']=token
                        if 'path' in values:values['path']=mapped(values['path'])
                        kw[part]=values
            return getattr(old_requests,method)(url,*args,**kw)
        def get(self,url,*a,**k):return self._call('get',url,*a,**k)
        def post(self,url,*a,**k):return self._call('post',url,*a,**k)
    ns['requests']=ScopedRequests()
    def response(body):
        if _NEW.get():
            body=str(body).replace(',record,/AI/in',',record,/9/AI/in')
        return original_response(body)
    ns['text_response']=response
    @app.route('/line-new/yemot-song',methods=['GET','POST'])
    def second_line_song():
        values=request.values.to_dict();extension=values.get('ApiExtension','').strip('/')
        if not secret or not hmac.compare_digest(values.get('secret',''),secret):return 'forbidden',403
        if extension not in {'9/2','9/2/9'}:return 'forbidden',403
        call=values.get('ApiCallId')
        if not call:return 'missing call',400
        values.update(secret=ns['BRIDGE_SECRET'],ApiCallId='new0772251131:'+call,ApiExtension='2')
        scope=_NEW.set(True)
        try:
            if extension=='9/2/9' and values.get('hangup')!='yes' and not any(re.fullmatch(r'(?:S|NM|BK)\d+|MODE|HOW',k)for k in values):
                rec=(ns['load_resume'](values.get('ApiPhone',''))or{}).get('2')
                if rec:ns['resume_pending'][values['ApiCallId']]={**rec,'ext':'2'}
            with app.test_request_context('/yemot-song',method='POST',data=values):
                result=original_song()
            if hasattr(result,'get_data'):
                result.set_data(re.sub(r'(go_to_folder=)/(?![A-Za-z0-9_])',r'\1/9',result.get_data(as_text=True)))
            return result
        finally:_NEW.reset(scope)
    @app.route('/line-new/storage-check',methods=['POST'])
    def second_line_storage_check():
        if not secret or not hmac.compare_digest(request.headers.get('X-Bridge-Secret',''),secret):return 'forbidden',403
        # Owned synthetic silence only. No URL/content input, no acquisition/deletion.
        import io,wave,hashlib,secrets
        stream=io.BytesIO()
        with wave.open(stream,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(8000);wav.writeframes(b'\0\0'*8000)
        data=stream.getvalue();name='owned_restore_'+secrets.token_hex(8)+'.wav';path='/9/2/'+name
        scope=_NEW.set(True)
        try:
            ns['ym_upload'](data,name,path)
            returned=ns['ym_download'](path)
            return {'ok':returned==data,'owned_synthetic_only':True,'bytes':len(data),'sha256':hashlib.sha256(returned).hexdigest(),'path':path},(200 if returned==data else 502)
        finally:_NEW.reset(scope)
    @app.route('/line-new/runtime-check',methods=['GET'])
    def second_line_runtime_check():
        if not secret or not hmac.compare_digest(request.headers.get('X-Bridge-Secret',''),secret):return 'forbidden',403
        counts={'old':{},'new':{}}
        with ns['lock']:
            for cid,job in ns['song_jobs'].items():
                group='new'if str(cid).startswith('new0772251131:')else'old'
                state=str(job.get('stage','unknown'));counts[group][state]=counts[group].get(state,0)+1
        return {'song_jobs':counts,'original_ai_sessions':len(ns['sessions'])}
    return {'context':_NEW,'map':mapped}
