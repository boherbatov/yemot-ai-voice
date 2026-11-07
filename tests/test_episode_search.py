import unittest,sys,threading,logging
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import episode_search as es
from flask import Flask,Response
class Immediate:
 def __init__(self,target,args=(),kwargs=None,**kw):self.target=target;self.args=args;self.kw=kwargs or {}
 def start(self):self.target(*self.args,**self.kw)
class Resp:
 url='https://example.com/feed';status_code=200
 def __init__(self,rows=None,xml=b''):self.rows=rows;self.xml=xml
 def raise_for_status(self):pass
 def json(self):return {'results':self.rows}
 def __enter__(self):return self
 def __exit__(self,*a):pass
 def iter_content(self,n):yield self.xml
class FakeReq:
 def __init__(self):self.calls=[];self.rows=[]
 def get(self,url,**kw):self.calls.append((url,kw));return Resp(self.rows,b'<rss/>')
class Search(unittest.TestCase):
 def setUp(self):
  self.app=Flask(__name__);self.req=FakeReq();self.jobs={};self.started=[]
  def original():
   from flask import request
   c=request.values.get('ApiCallId');self.jobs.setdefault(c,{'stage':'entry'})
   return Response('read=f-pod_entry=S1,no,1,1,7,No,no')
  self.app.add_url_rule('/pod',endpoint='yemot_pod',view_func=original,methods=['POST'])
  self.ns={'app':self.app,'BRIDGE_SECRET':'test','requests':self.req,'POD_HEADERS':{},'pod_jobs':self.jobs,'text_response':Response,'threading':type('T',(),{'Thread':Immediate}),'log':logging.getLogger(),'play_chain':lambda a,b:'read='+a+'='+b+',no,1,0,1,No,no','multitap_decode':lambda x,l:{'22':'word','33':'show'}.get(x,''),'tts_wav':lambda t:b'wav','ym_upload':lambda *a:{'success':True},'episode_items':lambda b:[{'id':'guid','title':'word episode','description':'details','url':'https://example.com/a.mp3','feed_index':0,'published':'2026-11-01'}],'start_episode':lambda c,j,e,t:self.started.append((c,j.copy(),e))or Response('PLAY')}
  es.install(self.ns);self.client=self.app.test_client()
 def call(self,**p):return self.client.post('/pod',data={'secret':'test','ApiCallId':'call',**p}).text
 def enter(self):self.call();return self.call(S1='4')
 def test_entry_append_old_menu(self):self.assertIn('pod_entry.f-ps_entry',self.call())
 def test_optional_show_allows_empty(self):self.assertIn('S2,no,120,0,20',self.enter());self.assertIn('ps_query',self.call(S2=''))
 def test_show_search_select(self):
  self.req.rows=[{'collectionName':'show','feedUrl':'https://example.com/feed'}];self.enter();self.call(S2='33');self.assertIn('EP4',self.call(S3='None'));self.assertIn('ps_query',self.call(EP4='1'));self.call(S5='22');self.call(S6='None');self.assertEqual(self.call(EP7='1'),'PLAY');self.assertEqual(self.started[0][2]['id'],'guid')
 def test_global_verifies_before_play(self):
  self.req.rows=[{'kind':'podcast-episode','episodeGuid':'guid','trackName':'word','collectionName':'show','feedUrl':'https://example.com/feed','episodeUrl':'https://example.com/a.mp3'}];self.enter();self.call(S2='');self.call(S3='22');self.call(S4='None');self.call(EP5='1');self.assertFalse(self.started);self.assertEqual(self.call(S6='None'),'PLAY')
 def test_bad_feed_rejected(self):self.assertFalse(es.public_url('http://127.0.0.1/a'));self.assertFalse(es.public_url('http://user:pass@example.com/a'));self.assertTrue(es.public_url('https://example.com/a'))
 def test_metadata_limit_and_cache(self):
  a=self.ns['_episode_search_test']['apple'];a('x','podcast',8);a('x','podcast',8);self.assertEqual(len(self.req.calls),1)
  for i in range(14):a(str(i),'podcast',8)
  with self.assertRaises(RuntimeError):a('over','podcast',8)
 def test_within_match_description_and_order(self):
  rows=[{'title':'A','description':'word'},{'title':'word title','description':''}];self.assertEqual(self.ns['_episode_search_test']['match'](rows,'word')[0]['title'],'word title');self.assertEqual(rows[0]['title'],'A')
 def test_no_match_does_not_play(self):self.enter();self.call(S2='');self.call(S3='22');self.assertIn('ps_none.f-ps_query',self.call(S4='None'));self.assertFalse(self.started)
 def test_native_selection_marker_unchanged(self):self.jobs['call']={'stage':'entry','_trial_gen':'keep'};self.call(S1='4');self.assertEqual(self.jobs['call']['_trial_gen'],'keep')
 def test_star_only_root_in_results(self):self.jobs['call']={'stage':'search_episode_pick'};self.assertEqual(self.call(EP1='*'),'go_to_folder=/')

class NavigationComposition(unittest.TestCase):
 call=Search.call
 def setUp(self):
  Search.setUp(self)
  import song_links
  song_links.install_navigation(self.ns)
 def test_numeric_selection_not_consumed_by_outer_back(self):
  self.req.rows=[{'collectionName':'show','feedUrl':'https://example.com/feed'}]
  initial=self.call();import re
  var=re.search(r'=(BK\d+),',initial).group(1)
  self.assertIn('ps_show',self.call(**{var:'4'}))
  self.call(S2='33');self.assertIn('EP4',self.call(S3='None'))
  self.assertIn('ps_query',self.call(EP4='1'))

if __name__=='__main__':unittest.main()
