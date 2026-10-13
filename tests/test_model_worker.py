import asyncio
import io
import json
import time
import uuid

import httpx
import pytest
from PIL import Image

from ghostdev.contracts import Action
from ghostdev.db import LeaseRevoked
from ghostdev.desktop import MacDesktop
from ghostdev.model import LocalModel
from ghostdev.worker import Worker
from conftest import request


def test_ollama_protocol_and_strict_local_response(environment):
    settings = environment[0]
    captured = []
    def handle(req):
        captured.append((req.url.path, json.loads(req.content)))
        if req.url.path == '/api/show':
            return httpx.Response(200,json={"capabilities":["completion"]})
        return httpx.Response(200,json={"model":settings.model_name,"message":{"content":json.dumps({"kind":"finish","observation_id":"abc","expected_app":"editor","summary":"Verify","postcondition":"Check saved output"})}})
    model = LocalModel(settings, httpx.MockTransport(handle))
    async def run():
        identity = await model.doctor()
        proposed, metric = await model.propose(request(),{"id":"abc"},None,[],{})
        assert not identity['hardware_verified'] and proposed.kind=='finish'
    asyncio.run(run())
    assert [entry[0] for entry in captured]==['/api/show','/api/chat']
    assert captured[1][1]['stream'] is False and captured[1][1]['format']['additionalProperties'] is False
    assert captured[1][1]['think'] is False and captured[1][1]['options']['num_ctx']==8192


def test_cloud_ollama_and_redirects_rejected(environment):
    settings = environment[0]
    for response in [httpx.Response(200,json={"remote_host":"https://ollama.com"}),httpx.Response(302,headers={"location":"https://evil.invalid"})]:
        model = LocalModel(settings,httpx.MockTransport(lambda req:response))
        with pytest.raises((ValueError,httpx.HTTPStatusError)):
            asyncio.run(model.doctor())


def test_text_model_cannot_silently_claim_vision(environment):
    settings=environment[0].model_copy(update={"model_vision":True})
    model=LocalModel(settings,httpx.MockTransport(lambda req:httpx.Response(200,json={"capabilities":["completion"]})))
    with pytest.raises(ValueError,match='vision'):
        asyncio.run(model.doctor())


class FakeDesktop(MacDesktop):
    """TEST ONLY. Never selectable in the shipped CLI or product configuration."""
    def __init__(self, settings):
        self.settings=settings;self.app='unapproved';self.image=None;self.note='';self.dispatched=[];self.observed=[]
    def observe(self, workspace):
        buffer=io.BytesIO();Image.new('RGB',(640,400),'#14262a').save(buffer,format='PNG');self.image=buffer.getvalue() if self.app!='unapproved' else None
        observation={"id":uuid.uuid4().hex,"timestamp":time.time(),"app":self.app,"capture_allowed":self.app!='unapproved',"image_width":640,"image_height":400,"bounds":{"X":0,"Y":0,"Width":640,"Height":400},"elements":[{"role":"AXStaticText","value":self.note}],"focus_role":"AXTextArea","signature":self.app+':'+self.note}
        self.observed.append(observation['id']);return observation
    def execute(self, action, observation, workspace, guard, path_resolver):
        guard();self.validate(action,observation);self.dispatched.append(action.kind)
        if action.kind=='launch_app':self.app=action.app
        if action.kind=='type_text':self.note=action.text
        if action.kind=='hotkey' and action.keys==['command','s']:path_resolver('GhostDev Report.txt').write_text(self.note)
        return {'dispatched':True,'test_adapter':True}


class FixtureModel:
    """A deterministic protocol fixture, NOT evidence of actual Mac inference."""
    def __init__(self):self.index=0
    async def doctor(self):return {'model':'test-fixture','hardware_verified':False}
    async def propose(self,task,observation,image,history,project):
        steps=[{'kind':'launch_app','app':'editor','path':'GhostDev Report.txt'}, {'kind':'click','x':100,'y':100}, {'kind':'type_text','text':task['expected_text']}, {'kind':'hotkey','keys':['command','s']}, {'kind':'start_preview'}, {'kind':'launch_app','app':'browser','path':next((h['result']['preview_url'] for h in history if 'result' in h and 'preview_url' in h['result']), '')}, {'kind':'scroll','amount':-5}, {'kind':'finish'}]
        fields=steps[self.index];self.index+=1
        action=Action(observation_id=observation['id'],expected_app=observation['app'],summary='Fixture step',postcondition='Fixture observation',**fields)
        return action,{'test_fixture':True}


def test_desktop_vertical_slice_durable_events_and_independent_verification(environment):
    settings,store,project=environment
    task=store.submit(request());store.claim()
    desktop=FakeDesktop(settings)
    asyncio.run(Worker(settings,store,desktop,FixtureModel()).run_task(task))
    result=store.task(task['id'])
    assert result['state']=='COMPLETED',result.get('error')
    assert result['final_screenshot'] and result['diff_hash']
    assert 'Local AI works' not in (project.root/'GhostDev Report.txt').read_text()
    assert {'click','type_text','scroll','launch_app'} <= set(desktop.dispatched)
    assert any(e['type']=='verification_finished' for e in store.events(task['id']))


def test_claiming_done_without_gui_evidence_fails(environment):
    settings,store,_=environment
    class Liar(FixtureModel):
        async def propose(self,task,observation,*args):
            return Action(kind='finish',observation_id=observation['id'],expected_app=observation['app'],summary='Done',postcondition='Done'),{}
    task=store.submit(request());store.claim()
    asyncio.run(Worker(settings,store,FakeDesktop(settings),Liar()).run_task(task))
    assert store.task(task['id'])['state']=='FAILED'
    assert 'Desktop acceptance' in store.task(task['id'])['error']


def test_cancel_during_inference_prevents_any_desktop_input(environment):
    settings,store,_=environment
    async def scenario():
        started=asyncio.Event();cancelled=asyncio.Event()
        class Pending(FixtureModel):
            async def propose(self,*args):
                started.set()
                try:await asyncio.sleep(100)
                finally:cancelled.set()
        task=store.submit(request());store.claim();desktop=FakeDesktop(settings)
        running=asyncio.create_task(Worker(settings,store,desktop,Pending()).run_task(task))
        await asyncio.wait_for(started.wait(),2)
        store.transition(task['id'],'CANCELLED',revoke=True)
        await asyncio.wait_for(running,2)
        assert cancelled.is_set() and desktop.dispatched==[]
        assert store.task(task['id'])['state']=='CANCELLED'
    asyncio.run(scenario())


def test_pause_resume_discards_pending_inference_and_observes_again(environment):
    settings,store,_=environment
    async def scenario():
        started=asyncio.Event();calls=[]
        class Pending(FixtureModel):
            async def propose(self,task,observation,*args):
                calls.append(observation['id'])
                if len(calls)==1:
                    started.set();await asyncio.sleep(100)
                return Action(kind='finish',observation_id=observation['id'],expected_app=observation['app'],summary='Inspect complete',postcondition='No mutation'),{}
        task=store.submit(request(task_type='inspect'));store.claim();desktop=FakeDesktop(settings)
        running=asyncio.create_task(Worker(settings,store,desktop,Pending()).run_task(task))
        await asyncio.wait_for(started.wait(),2)
        store.transition(task['id'],'PAUSED',revoke=True)
        await asyncio.sleep(.2)
        store.transition(task['id'],'RUNNING',revoke=True)
        await asyncio.wait_for(running,2)
        assert len(calls)==2 and calls[0]!=calls[1] and desktop.dispatched==[]
        assert store.task(task['id'])['state']=='COMPLETED'
    asyncio.run(scenario())


def test_task_deadline_cancels_pending_model_request(environment):
    settings,store,_=environment
    async def scenario():
        task=store.submit(request());store.claim()
        worker=Worker(settings,store,FakeDesktop(settings),FixtureModel())
        with pytest.raises(TimeoutError):
            await worker.guarded(asyncio.sleep(100),task['id'],0,time.monotonic()+.1)
    asyncio.run(scenario())


def test_pause_during_preflight_is_resumable(environment):
    settings,store,_=environment
    async def scenario():
        started=asyncio.Event();calls=[]
        class Preflight(FixtureModel):
            async def doctor(self):
                calls.append(1)
                if len(calls)==1:
                    started.set();await asyncio.sleep(100)
                return {'model':'test-fixture','hardware_verified':False}
            async def propose(self,task,observation,*args):
                return Action(kind='finish',observation_id=observation['id'],expected_app=observation['app'],summary='Inspect',postcondition='No changes'),{}
        task=store.submit(request(task_type='inspect'));store.claim()
        running=asyncio.create_task(Worker(settings,store,FakeDesktop(settings),Preflight()).run_task(task))
        await asyncio.wait_for(started.wait(),2)
        store.transition(task['id'],'PAUSED',revoke=True)
        await asyncio.sleep(.2)
        store.transition(task['id'],'RUNNING',revoke=True)
        await asyncio.wait_for(running,2)
        assert len(calls)==2 and store.task(task['id'])['state']=='COMPLETED'
    asyncio.run(scenario())


def test_local_openai_compatible_path_and_schema(environment):
    settings=environment[0].model_copy(update={'model_backend':'openai-compatible','model_url':'http://127.0.0.1:18000/v1'})
    paths=[]
    def handle(req):
        paths.append(req.url.path)
        if req.url.path.endswith('/models'):
            return httpx.Response(200,json={'data':[{'id':settings.model_name}]})
        payload=json.loads(req.content)
        assert payload['response_format']['json_schema']['name']=='desktop_action'
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'kind':'finish','observation_id':'abc','expected_app':'editor','summary':'Verify','postcondition':'Check output'})}}]})
    model=LocalModel(settings,httpx.MockTransport(handle))
    async def scenario():
        await model.doctor();await model.propose(request(),{'id':'abc'},None,[],{})
    asyncio.run(scenario())
    assert paths==['/v1/models','/v1/chat/completions']
