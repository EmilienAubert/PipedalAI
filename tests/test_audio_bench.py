from __future__ import annotations
import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock
import numpy as np
import soundfile as sf
from pipedal_ai.audio_io import audio_levels, digest_file, preview_audio, safe_path, write_pcm
from pipedal_ai.config import BenchConfig
from pipedal_ai.di import ingest_di
from pipedal_ai.errors import ContractError
from pipedal_ai.pi.bench import BenchService, optimization_grid
from pipedal_ai.rtx.audio_analysis import analyze_pair, multilevel_summary, score_features
from pipedal_ai.rtx.audio_api import AudioPairRequest, decode_audio_pair
from pipedal_ai.intent import fallback_tone_intent


def signal():
    t=np.arange(48000*2)/48000
    envelope=.025+.17*(.5+.5*np.sin(2*np.pi*1.7*t))
    return (envelope*np.sin(2*np.pi*440*t)).astype('float32')[:,None]


def manifest():
    return {'schema_version':'pipedal-ai.di-manifest/1.0.0','set_id':'test_DI','sample_rate_hz':48000,'pcm_bits':24,'channels':1,
      'files':[{'di_id':'dynamics','file':'di.wav','purpose':'dynamics','guitar':'Strat','pickup_type':'single_coil','pickup_position':'bridge',
        'guitar_volume':10,'guitar_tone':10,'input_gain_note':'Hi-Z gain fixed','performance':'Soft/strong picking','notes':''}]}


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.di=self.root/'di.wav';write_pcm(self.di,signal())

    def test_alignment_and_linear_dynamic_response(self):
        output=self.root/'output.wav';write_pcm(output,np.concatenate([np.zeros((4800,1)),signal()*.8]))
        result=analyze_pair(self.di,output)
        self.assertAlmostEqual(result['alignment_seconds'],.1,delta=.021)
        self.assertGreater(result['alignment_confidence'],.9)
        self.assertAlmostEqual(result['envelope_slope'],1,delta=.04)
        self.assertLess(result['features']['compression_proxy'],.05)

    def test_saturation_proxy_and_silence_rejection(self):
        output=self.root/'output.wav';write_pcm(output,np.tanh(signal()*15)*.25)
        result=analyze_pair(self.di,output)
        self.assertGreater(result['features']['compression_proxy'],.25)
        silence=self.root/'silence.wav';write_pcm(silence,np.zeros_like(signal()))
        with self.assertRaisesRegex(ValueError,'silencieux'):
            analyze_pair(self.di,silence)

    def test_preview_is_peak_limited_and_does_not_modify_render(self):
        digest=digest_file(self.di)
        result=preview_audio(self.di,self.root/'preview.wav')
        self.assertLessEqual(result['levels']['peak_dbfs'],-3+.001)
        self.assertEqual(digest_file(self.di),digest)

    def test_audio_transport_verifies_each_hash(self):
        raw=self.di.read_bytes()
        request=AudioPairRequest(schema_version='pipedal-ai.audio-pair/1.0.0',request_id='pair_test',catalog={'revision':1,'sha256':'a'*64},
           di_sha256=digest_file(self.di),render_sha256=digest_file(self.di),di_wav=base64.b64encode(raw).decode(),render_wav=base64.b64encode(raw).decode())
        response=decode_audio_pair(request)
        self.assertEqual(response['analysis']['di_sha256'],request.di_sha256)
        request.render_sha256='0'*64
        with self.assertRaisesRegex(ValueError,'SHA-256'):
            decode_audio_pair(request)

    def test_scores_reject_non_finite_values(self):
        with self.assertRaises(ValueError):
            score_features({'warmth':float('nan')},fallback_tone_intent('warm crunch'))

    def test_multilevel_requires_multiple_actual_levels(self):
        analysis=analyze_pair(self.di,self.di)
        with self.assertRaisesRegex(ValueError,'deux niveaux'):
            multilevel_summary([{'input_gain_db':0,'analysis':analysis}])
        stronger=json.loads(json.dumps(analysis));stronger['render_levels']['rms_dbfs']+=3
        summary=multilevel_summary([{'input_gain_db':0,'analysis':analysis},{'input_gain_db':6,'analysis':stronger}])
        self.assertAlmostEqual(summary['level_response_slope'],.5,places=3)


class DIAndBenchTests(unittest.TestCase):
    def setUp(self):
        from test_core import CoreTests
        self.fixture=CoreTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.root=self.fixture.root;self.db=self.fixture.db;self.catalog=self.fixture.catalog
        self.di=self.root/'di.wav';write_pcm(self.di,signal())
        self.manifest=self.root/'manifest.json';self.manifest.write_text(json.dumps(manifest()),encoding='utf-8')
        self.storage=self.root/'di-sets'

    def ingest(self):
        return ingest_di(self.db,self.manifest,self.storage)

    def test_di_ingestion_is_idempotent_and_content_pinned(self):
        first=self.ingest();second=self.ingest()
        self.assertEqual(first['sha256'],second['sha256'])
        sf.write(self.di,signal()*.5,48000,subtype='PCM_24')
        with self.assertRaisesRegex(ValueError,'autre contenu'):
            self.ingest()

    def test_di_refuses_clipping_wrong_format_and_traversal(self):
        sf.write(self.di,np.ones((96000,1)),48000,subtype='PCM_24')
        with self.assertRaisesRegex(ValueError,'écrêt'):
            self.ingest()
        sf.write(self.di,signal(),48000,subtype='PCM_16')
        with self.assertRaisesRegex(ValueError,'24 bits'):
            self.ingest()
        value=manifest();value['files'][0]['file']='../di.wav';self.manifest.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'non sûr'):
            self.ingest()

    def test_safe_paths_reject_symlinks_and_parent_escape(self):
        with self.assertRaises(ValueError):safe_path(self.root,'../outside')
        linked=self.root/'linked.wav'
        try:linked.symlink_to(self.di)
        except OSError:self.skipTest('Symlink privileges unavailable')
        with self.assertRaisesRegex(ValueError,'symbolique'):safe_path(self.root,'linked.wav')

    def bench(self, clipped=False):
        from pipedal_ai.compiler import PresetCompiler
        from pipedal_ai.degraded import DegradedProposer
        self.ingest()
        proposal=DegradedProposer().propose('job_audio','Son blues chaud',self.catalog.capabilities())
        for spec in proposal.proposals:
            amp=next(s for s in spec.chain if s.resources);amp.instance_id='amp'
        with self.db.transaction() as c:
            c.execute("INSERT INTO jobs(job_id,kind,status,source,prompt,catalog_revision,catalog_sha256,request_json,proposal_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
             ('job_audio','text','completed','degraded','Son blues chaud',1,'a'*64,'{}',proposal.model_dump_json(),'now','now'))
        async def render(spec,di,target,**kwargs):
            write_pcm(target,np.clip(signal()*20,-1,1) if clipped else signal()*.8)
            return {'renderer_version':'test','telemetry':[]}
        async def analyze(di,target,cat,identifier):return analyze_pair(di,target)
        renderer=Mock();renderer.render=AsyncMock(side_effect=render)
        rtx=Mock();rtx.analyze_pair=AsyncMock(side_effect=analyze)
        config=BenchConfig(enabled=True,di_root=self.storage,output_root=self.root/'bench',max_renders=3)
        compiler=PresetCompiler(self.catalog,self.root/'uploads',1000000)
        return BenchService(self.db,self.catalog,compiler,renderer,rtx,config)

    def test_original_three_measured_before_optimization_and_exports_verified(self):
        bench=self.bench()
        report=asyncio.run(bench.evaluate_job('job_audio','test_DI',maintenance_confirmed=True))
        self.assertEqual(len(report['candidates']),3)
        self.assertTrue(all(c['original'] for c in report['candidates']))
        self.assertEqual(len(report['optimized_artifacts']),3)
        self.assertEqual(bench.session(report['session_id'])['status'],'completed')
        path=bench.preview(report['candidates'][0]['candidate_id']);path.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError,'Préécoute modifiée'):bench.preview(report['candidates'][0]['candidate_id'])

    def test_clipped_renders_never_export_and_maintenance_gate(self):
        bench=self.bench(clipped=True)
        with self.assertRaises(ContractError):asyncio.run(bench.evaluate_job('job_audio','test_DI'))
        report=asyncio.run(bench.evaluate_job('job_audio','test_DI',maintenance_confirmed=True,optimize=False))
        self.assertEqual(report['optimized_artifacts'],[])

    def test_preference_feedback_is_scoped_to_named_profile(self):
        from pipedal_ai.models import GuitarProfileCreate
        from pipedal_ai.preferences import Feedback, record_feedback, ranking_preferences
        bench=self.bench()
        with self.db.transaction() as c:
            c.execute('INSERT INTO guitar_profiles VALUES(?,?,?,?,?,?,?)',('gtr_test','Strat','Strat','single_coil',0,'','now'))
            c.execute('UPDATE jobs SET profile_id=? WHERE job_id=?',('gtr_test','job_audio'))
        record_feedback(self.db,self.catalog,Feedback(job_id='job_audio',variant='balanced',label='preferred'))
        self.assertEqual(ranking_preferences(self.db,None),[])
        self.assertEqual(ranking_preferences(self.db,'gtr_other'),[])
        self.assertGreater(ranking_preferences(self.db,'gtr_test')[0]['score'],0)

    def test_characterization_reuses_cache_and_exports_no_render_journal_paths(self):
        bench=self.bench()
        first=asyncio.run(bench.characterize('test_DI',nam_limit=1,maintenance_confirmed=True))
        self.assertEqual(len(first['profiles']),1)
        self.assertEqual(bench.renderer.render.await_count,3)
        second=asyncio.run(bench.characterize('test_DI',nam_limit=1,maintenance_confirmed=True))
        self.assertTrue(second['profiles'][0]['cached'])
        self.assertEqual(bench.renderer.render.await_count,3)
        cap=self.catalog.capabilities()
        self.assertNotIn(str(self.root),json.dumps(cap))
        profile=next(iter(cap['assets'][0]['metadata']['characterization']['profiles'].values()))
        self.assertIn('saturation_proxy',profile['features'])
        self.assertNotIn('measurements',profile)

    def test_antiphase_stereo_keeps_energy(self):
        path=self.root/'stereo.wav';write_pcm(path,np.concatenate((signal(),-signal()),axis=1))
        result=analyze_pair(self.di,path)
        self.assertGreater(result['alignment_confidence'],.9)
