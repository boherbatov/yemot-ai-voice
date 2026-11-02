"""Offline extracted-function tests: no credentials, startup, media or network.
Real threads exercise menu/prefetch overlap; fake clocks cover readiness timing.
"""
import ast, io, logging, re, threading, time, types, unittest
from pathlib import Path
TREE=ast.parse(Path(__file__).resolve().parents[1].joinpath('app.py').read_text())
NAMES={'fetch_results','speculative_prefetch','reuse_or_start_prefetch','fetch_queue_song','_block_until','_after_pick'}
CODE=compile(ast.Module(body=[n for n in TREE.body if isinstance(n,ast.FunctionDef) and n.name in NAMES],type_ignores=[]),'<latency functions>','exec')
RESULTS=[('abcdefghijk','first'),('gd:second','second')]
def env():
    job={'stage':'searching','kind':'song','status':'working'};events=[]
    e={'re':re,'time':time,'threading':threading,'song_jobs':{'c':job},'YT_REFRESH_TOKEN':True,
       'log':logging.getLogger('latency-test'),'_ai_clean_query':lambda *a:None,
       'merged_song_search':lambda *a,**k:list(RESULTS),'_results_look_poor':lambda *a:False,
       'prefetch_extras':lambda *a:events.append(('extras',job.get('gen'))),
       'start_prefetch':lambda c,i,force=False:events.append(('first',job.get('gen'))),
       'cancel_yt_except':lambda *a:None,'_adopt_extra':lambda *a:False,
       'SONG_POLL_BLOCK_S':2.,'SONG_READY_BLOCK_S':3.,'_song_loading_ok':True,'play_chain':lambda a,b:a,
       'text_response':lambda x:x,'wait_step':lambda *a:'play'}
    def menu(c,j,r,*a):
        events.append(('menu',None));j['gen']=j.get('gen',0)+1;j['status']='ready';return ['page']
    e['publish_result_pages']=menu;exec(CODE,e);return e,job,events
class Pipeline(unittest.TestCase):
    def test_first_before_menu_extras_after_generation(self):
        e,j,a=env();e['fetch_results']('c','q')
        self.assertEqual(a,[('first',None),('menu',None),('extras',1)])
    def test_menu_failure_invalidates_worker_and_starts_no_extras(self):
        e,j,a=env();e['publish_result_pages']=lambda *a:(_ for _ in ()).throw(ValueError('fake menu fail'))
        e['fetch_results']('c','q');self.assertEqual(j['status'],'error')
        self.assertIsNone(j['slot0_vwant']);self.assertEqual(len(a),1)
    def test_first_can_finish_while_menu_is_blocked_real_thread(self):
        e,j,a=env();done=threading.Event();threads=[]
        def start(*a,**k):
            def work():j['slot0_status']='ready';done.set()
            t=threading.Thread(target=work);threads.append(t);t.start()
        e['start_prefetch']=start
        def menu(*a):
            self.assertTrue(done.wait(1));j.update(gen=1,status='ready');return ['page']
        e['publish_result_pages']=menu;e['fetch_results']('c','q')
        for t in threads:t.join(1)
        self.assertEqual(j['slot0_status'],'ready');self.assertEqual(a,[('extras',1)])
    def test_left_call_no_first_worker(self):
        e,j,a=env();e['song_jobs'].clear();e['speculative_prefetch']('c',j,RESULTS,extras=False)
        self.assertEqual(a,[])
    def test_first_only_does_not_start_extra_workers(self):
        e,j,a=env();e['speculative_prefetch']('c',j,RESULTS,extras=False)
        self.assertEqual(a,[('first',None)])
    def test_reuse_ready_first(self):
        e,j,a=env();j.update(queue=RESULTS[:1],slot0_status='ready',slot0_want=0,slot0_vwant=RESULTS[0][0])
        e['reuse_or_start_prefetch']('c',0);self.assertEqual(a,[])
    def test_other_choice_retargets(self):
        e,j,a=env();j.update(queue=RESULTS[1:],slot0_status='working',slot0_want=0,slot0_vwant=RESULTS[0][0])
        e['reuse_or_start_prefetch']('c',0);self.assertEqual(a,[('first',None)])
class WorkerRaces(unittest.TestCase):
    def setup_worker(self):
        e,j,a=env();j.update(queue=RESULTS[:1],slot0_want=0,slot0_vwant=RESULTS[0][0],mode='single')
        e.update(SongTooLongError=type('TooLong',(Exception,),{}),DiskLowError=type('Disk',(Exception,),{}),
          SEGMENT_LONG=False,SONG_DIR='/2',_download_convert=lambda *a:('first','fake.wav'),
          slot_name=lambda *a:'slot',ym_delete=lambda *a:None,ym_upload=lambda *a:None,
          open=lambda *a,**k:io.BytesIO(b'fake'),_cleanup_tmp=lambda *a:None)
        return e,j
    def test_replaced_call_before_upload_not_published(self):
        e,j=self.setup_worker()
        def download(*a):e['song_jobs']['c']={};return 'first','fake.wav'
        e['_download_convert']=download;e['fetch_queue_song']('c',0)
        self.assertNotEqual(j.get('slot0_status'),'ready')
    def test_menu_fails_during_upload_not_published(self):
        e,j=self.setup_worker();entered=threading.Event();finish=threading.Event()
        def upload(*a):entered.set();self.assertTrue(finish.wait(1))
        e['ym_upload']=upload;t=threading.Thread(target=e['fetch_queue_song'],args=('c',0));t.start()
        self.assertTrue(entered.wait(1));j.update(slot0_want=None,slot0_vwant=None);finish.set();t.join(1)
        self.assertFalse(t.is_alive());self.assertNotEqual(j.get('slot0_status'),'ready')
    def test_changed_pick_during_upload_not_published(self):
        e,j=self.setup_worker();e['ym_upload']=lambda *a:j.update(slot0_vwant='different')
        e['fetch_queue_song']('c',0);self.assertNotEqual(j.get('slot0_status'),'ready')
class Readiness(unittest.TestCase):
    def clock(self,ready):
        e,j,a=env();clock=types.SimpleNamespace(now=0.)
        def sleep(s):clock.now+=s
        e['time']=types.SimpleNamespace(time=lambda:clock.now,sleep=sleep)
        return e,clock,lambda:clock.now>=ready
    def test_observed_2point127_boundary_fits_three_second_hold(self):
        e,c,p=self.clock(2.127);self.assertTrue(e['_block_until'](p,3));self.assertLess(c.now,2.3)
    def test_old_two_second_hold_misses_observed_boundary(self):
        e,c,p=self.clock(2.127);self.assertFalse(e['_block_until'](p,2))
    def test_never_ready_remains_bounded(self):
        e,c,p=self.clock(100);self.assertFalse(e['_block_until'](p,3));self.assertLess(c.now,3.11)
    def test_immediately_ready_no_hold(self):
        e,c,p=self.clock(0);self.assertTrue(e['_block_until'](p,3));self.assertEqual(c.now,0)
    def test_after_pick_fast_ready_uses_play_not_wait_prompt(self):
        e,j,a=env();j['slot0_status']='ready';self.assertEqual(e['_after_pick']('c',j,1),'play')
if __name__=='__main__':unittest.main()
