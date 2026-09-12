from __future__ import annotations
import asyncio
import base64
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import httpx
from test_audio_bench import signal, manifest
from pipedal_ai.audio_io import write_pcm
from pipedal_ai.config import load_pi_config
from pipedal_ai.pi.app import create_app


class BenchAPITests(unittest.TestCase):
    def setUp(self):
        from test_core import CoreTests
        self.fixture=CoreTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        root=self.fixture.root
        path=root/'pi.toml'
        path.write_text('[server]\nhost="127.0.0.1"\napi_key_env="TEST_BENCH_API_KEY"\n[storage]\n'+
            'database='+json.dumps(str(self.fixture.db.path))+'\nartifact_root='+json.dumps(str(root/'artifacts'))+'\nupload_root='+json.dumps(str(root/'uploads'))+'\n'+
            '[rtx]\nenabled=false\n[bench]\ndi_root='+json.dumps(str(root/'di-store'))+'\noutput_root='+json.dumps(str(root/'bench'))+'\n',encoding='utf-8')
        with patch.dict('os.environ',{'TEST_BENCH_API_KEY':'test-key'}):config=load_pi_config(path)
        self.app=create_app(config);self.headers={'X-PiPedal-AI-Key':'test-key'}
        audio=root/'di.wav';write_pcm(audio,signal())
        self.payload={'manifest':manifest(),'files':[{'di_id':'dynamics','audio_base64':base64.b64encode(audio.read_bytes()).decode()}]}
    def request(self,method,path,body=None,headers=None):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(self.app,client=('127.0.0.1',50000)),base_url='http://127.0.0.1') as client:
                return await client.request(method,path,json=body,headers=headers or {})
        return asyncio.run(run())
    def test_di_routes_require_key_and_validate_audio(self):
        self.assertEqual(self.request('POST','/api/v1/di/import',self.payload).status_code,401)
        result=self.request('POST','/api/v1/di/import',self.payload,self.headers)
        self.assertEqual(result.status_code,201,result.text)
        listed=self.request('GET','/api/v1/di',headers=self.headers)
        self.assertEqual(listed.json()[0]['set_id'],'test_DI')
    def test_bad_wav_is_422_not_server_error(self):
        self.payload['files'][0]['audio_base64']=base64.b64encode(b'not WAV').decode()
        result=self.request('POST','/api/v1/di/import',self.payload,self.headers)
        self.assertEqual(result.status_code,422,result.text)
    def test_bench_cannot_run_when_disabled(self):
        result=self.request('POST','/api/v1/bench/evaluate',{'job_id':'job_test','set_id':'test_DI','maintenance_confirmed':True},self.headers)
        self.assertEqual(result.status_code,409)
    def test_profile_calibration_is_private_explicit_data(self):
        result=self.request('POST','/api/v1/profiles',{'name':'Measured Strat','nam_input_calibration_dbu':-4.2},self.headers)
        self.assertEqual(result.status_code,201,result.text)
        listed=self.request('GET','/api/v1/profiles',headers=self.headers)
        self.assertEqual(listed.json()[0]['nam_input_calibration_dbu'],-4.2)
