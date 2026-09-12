from __future__ import annotations
import asyncio
from copy import deepcopy
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch
from test_audio_bench import signal
from test_core import descriptor
from pipedal_ai.audio_io import write_pcm
from pipedal_ai.compiler import PresetCompiler
from pipedal_ai.config import BenchConfig
from pipedal_ai.degraded import DegradedProposer
from pipedal_ai.errors import ContractError, RemoteServiceError
from pipedal_ai.knowledge import TOOB
from pipedal_ai.pi.renderer import PiPedalRenderer, process_lock


class FakePiPedal:
    def __init__(self, data, failure=None):
        self.previous={'name':'Unsaved live board','items':[{'instanceId':1,'uri':'user-plugin','controlValues':[{'key':'gain','value':.3}]}],
                       'input_volume_db':-4,'output_volume_db':-7}
        self.board=deepcopy(self.previous);self.data=data;self.failure=failure;self.requests=[];self.status_calls=0
    def connection(self,*args,**kwargs):return self
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def send(self,raw):
        header,body=json.loads(raw)
        if header['message']=='updateCurrentPedalboard':
            if body['pedalboard']['name'].startswith('PiPedal AI bench'):
                assert not {i['instanceId'] for i in body['pedalboard']['items']} & {i['instanceId'] for i in self.previous['items']}, 'Live effects would be borrowed by ID'
            self.board=deepcopy(body['pedalboard'])
        if header['message']=='setControl' and body['symbol']=='play' and self.failure=='connection':
            raise RemoteServiceError('Connection lost')
        if header['message']=='setControl' and body['symbol']=='stop' and body['instanceId']>1:
            rec=self.board['items'][-1]
            path=next(iter(rec['lv2State'][1].values()))['value']
            if not Path(path).exists():write_pcm(Path(path),self.data*.8)
    async def request(self,ws,message,body=None,sequence=1):
        self.requests.append(message)
        if message=='currentPedalboard':return deepcopy(self.board)
        if message=='getJackStatus':
            self.status_calls+=1
            if self.status_calls==2 and self.failure=='user':self.board=deepcopy(self.previous);self.board['name']='User selected another'
            return {'active':True,'underruns':1 if self.status_calls==2 and self.failure=='xrun' else 0,'cpuUsage':15}
        raise AssertionError(message)


class RendererTests(unittest.TestCase):
    def setUp(self):
        from test_core import CoreTests
        self.fixture=CoreTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.root=self.fixture.root;self.catalog=self.fixture.catalog
        with self.fixture.db.transaction() as c:
            for number,suffix,symbols in ((4,'player',['stop','pause','play','volIn','volFile']),
                                         (5,'record-mono',['stop','record','play','fformat','level'])):
                controls=[dict(symbol=s,datatype='number',minimum=-40 if s.startswith('vol') or s=='level' else 0,maximum=30 if s.startswith('vol') or s=='level' else 3,default=0) for s in symbols]
                value=descriptor(TOOB+suffix,'TooB '+suffix,controls)
                c.execute('INSERT INTO catalog_plugins VALUES(1,?,?,?,?,?,?,?,?)',('plg_'+str(number)*24,TOOB+suffix,'TooB '+suffix,'Plugin','test',0,'c'*64,value))
        self.di=self.root/'di.wav';write_pcm(self.di,signal())
        self.spec=DegradedProposer().propose('renderer','warm crunch',self.catalog.capabilities()).proposals[1]
        self.config=BenchConfig(enabled=True,output_root=self.root/'bench')
    def run_render(self,failure=None):
        fake=FakePiPedal(signal(),failure)
        client=Mock();client.config.websocket_url='ws://local.test/pipedal';client._request=fake.request
        renderer=PiPedalRenderer(self.catalog,PresetCompiler(self.catalog,self.root/'uploads',1000000),client,self.config,Mock())
        moments=[0,99] if failure not in ('xrun','user') else [0,0,0,99]
        # Simulated transport is portable; the real Pi lock is tested separately.
        with patch('pipedal_ai.pi.renderer.process_lock',return_value=nullcontext()),patch('pipedal_ai.pi.renderer.connect',side_effect=fake.connection),patch('pipedal_ai.pi.renderer.asyncio.sleep',new=AsyncMock()),patch('pipedal_ai.pi.renderer.monotonic',side_effect=moments):
            try:result=asyncio.run(renderer.render(self.spec,self.di,self.root/'result.wav',maintenance_confirmed=True))
            except Exception as exc:result=exc
        return fake,result
    def test_restores_entire_unsaved_board_and_records_local_audio(self):
        fake,result=self.run_render()
        self.assertIsInstance(result,dict,result)
        self.assertEqual(fake.board,fake.previous)
        self.assertEqual(result['physical_output_db'],-96)
        self.assertTrue((self.root/'result.wav').exists())
    def test_connection_failure_restores_using_fresh_connection(self):
        fake,result=self.run_render('connection')
        self.assertIsInstance(result,RemoteServiceError)
        self.assertEqual(fake.board,fake.previous)
    def test_xrun_aborts_and_restores(self):
        fake,result=self.run_render('xrun')
        self.assertIsInstance(result,ContractError)
        self.assertIn('Décrochage',str(result));self.assertEqual(fake.board,fake.previous)
    def test_user_selected_board_is_never_overwritten_by_restoration(self):
        fake,result=self.run_render('user')
        self.assertIsInstance(result,ContractError)
        self.assertEqual(fake.board['name'],'User selected another')
    @unittest.skipUnless(sys.platform.startswith('linux'), 'Verrou fcntl du banc Pi : Linux uniquement')
    def test_cross_process_lock_rejects_second_renderer(self):
        with process_lock(self.root/'locks'):
            with self.assertRaisesRegex(ContractError,'déjà actif'):
                with process_lock(self.root/'locks'):pass
    def test_real_renderer_rejects_windows_before_creating_lock_files(self):
        root = self.root/'unsupported-locks'
        with patch('pipedal_ai.pi.renderer.sys.platform', 'win32'):
            with self.assertRaisesRegex(ContractError, 'nécessite Linux'):
                with process_lock(root):pass
        self.assertFalse(root.exists())
