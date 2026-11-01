"""Credential-free route tests. Extract only functions; no app startup or network."""
import ast
import logging
import re
import threading
import time
import types
import unittest
from pathlib import Path

source = Path(__file__).resolve().parents[1].joinpath('app.py').read_text()
tree = ast.parse(source)
functions = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in ('yemot_song', 'multitap_decode', 'multitap_read'):
        node.decorator_list = []
        functions.append(node)

def route_env():
    calls = []
    class FakeThread:
        def __init__(self, target, args=(), **kw): self.target, self.args = target, args
        def start(self): calls.append((self.target.__name__, self.args))
    env = dict(log=logging.getLogger("unit"), re=re, time=time, request=types.SimpleNamespace(values={}), BRIDGE_SECRET='test-only',
               lock=threading.Lock(), song_jobs={}, resume_pending={}, resume_take=lambda *a: None,
               text_response=lambda x: x, IN_DIR='/in', GOODBYE_WORDS=[],
               MT_HE={'3':'א','33':'ב','333':'ג','0':' '}, MT_EN={},
               threading=types.SimpleNamespace(Thread=FakeThread), play_chain=lambda p,v: p+'='+v,
               fetch_general_results=lambda *a: None)
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<isolated-route>', 'exec'),env)
    env['spawned'] = calls
    return env

class GeneralTyping(unittest.TestCase):
    def setUp(self): self.e = route_env()
    def call(self, **params):
        self.e['request'].values = dict(secret='test-only',ApiCallId='unit',**params)
        return self.e['yemot_song']()
    def test_key4_uses_existing_keypad_not_recording(self):
        out=self.call(MODE='4')
        self.assertIn('read=f-song_gen_type=S1,no,120',out)
        self.assertNotIn('record',out)
        self.assertEqual(self.e['song_jobs']['unit']['stage'],'gen_typed')
    def test_keypad_starts_general_search(self):
        self.call(MODE='4');self.call(MODE='4',S1='3*33*3')
        job=self.e['song_jobs']['unit']
        self.assertEqual((job['kind'],job['query'],job['stage']),('general','אבא','searching'))
        self.assertEqual(self.e['spawned'][0][1],('unit','אבא'))
    def test_empty_and_recording_path_reprompt_without_stt(self):
        self.call(MODE='4')
        for val in ('','None','/in/recording.wav','***'):
            out=self.call(MODE='4',S1=val)
            self.assertIn('f-song_gen_type=S2,no,120',out)
            self.assertEqual(self.e['spawned'],[])
    def test_keys1_and2_still_offer_original_how_menu(self):
        for mode in ('1','2'):
            self.assertIn('f-song_how=HOW',self.call(MODE=mode))
    def test_key3_still_voice_ai(self):
        self.assertIn('f-song_ai_ask=S1,no,record',self.call(MODE='3'))
    def test_no_credential_rejected(self):
        self.e['request'].values={}
        self.assertEqual(self.e['yemot_song'](),('forbidden',403))

if __name__=='__main__':unittest.main()
