import json
import time

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from ghostdev.contracts import Action
from ghostdev.tools import CodeTools, Preview
from ghostdev.workspace import Workspace
from conftest import pair, request


def test_preview_is_loopback_scoped_and_blocks_external_connections(environment):
    settings,store,project=environment
    project=project.model_copy(update={'preview_file':'report.html'})
    ws=Workspace(settings.state_dir/'tasks'/'abc',project);ws.prepare(True)
    ws.path(project.entry_file).write_text('<script>alert(1)</script> Exact note')
    preview=Preview(ws)
    try:
        with httpx.Client(trust_env=False) as client:
            result=client.get(preview.url)
            assert result.status_code==200 and '&lt;script&gt;' in result.text
            assert "connect-src 'none'" in result.headers['content-security-policy']
            assert client.get(preview.url+'%2e%2e/config.json').status_code==404
            assert client.get(preview.url+'.env').status_code==404
    finally:preview.close()


def test_no_docker_never_runs_project_on_host(environment,monkeypatch):
    settings,store,project=environment
    task=store.submit(request(task_type='coding'))
    ws=Workspace(settings.state_dir/'tasks'/task['id'],project);ws.prepare(True)
    monkeypatch.setattr('ghostdev.tools.shutil.which',lambda command:None)
    with pytest.raises(RuntimeError,match='host-shell fallback is disabled'):
        CodeTools(store,ws,task['id']).run_check('cart',lambda:None)


def test_reviewed_apply_endpoint_rejects_conflicts_and_preserves_state(client,environment):
    settings,store,project=environment;pair(client,store)
    task=store.submit(request(task_type='coding'));store.claim()
    ws=Workspace(settings.state_dir/'tasks'/task['id'],project);ws.prepare(True)
    ws.patch('cart.js','return price;','return price * quantity;')
    store.update(task['id'],diff_hash=ws.diff()['sha256'])
    store.transition(task['id'],'VERIFYING');store.transition(task['id'],'READY_FOR_REVIEW')
    (project.root/'cart.js').write_text('Human edit')
    result=client.post(f"/tasks/{task['id']}/apply",json={'diff_hash':ws.diff()['sha256']})
    assert result.status_code==409
    assert store.task(task['id'])['state']=='READY_FOR_REVIEW'
    assert (project.root/'cart.js').read_text()=='Human edit'


def test_authorized_artifact_and_websocket_reconnect_sequence(client,environment,tmp_path):
    _,store,_=environment;pair(client,store)
    task=store.submit(request());store.event(task['id'],'example',{'real':True})
    with client.websocket_connect(f"ws://localhost:8765/tasks/{task['id']}/stream?after_seq=1",headers={'origin':'http://localhost:8765'}) as socket:
        assert socket.receive_json()['seq']==2
    path=environment[0].state_dir/'artifact.txt';path.write_text('Bounded output')
    artifact=store.artifact(task['id'],path,'application/octet-stream','output.txt')
    assert client.get('/artifacts/'+artifact).content==b'Bounded output'
    assert client.get('/artifacts/arbitrary-host-path').status_code==404


def test_dirty_git_project_is_inspection_only(environment):
    import subprocess
    settings,_,project=environment
    for command in [['git','init',str(project.root)],['git','-C',str(project.root),'add','.'],['git','-C',str(project.root),'-c','user.name=GhostDev Test','-c','user.email=test@example.invalid','commit','-m','fixture']]:
        subprocess.run(command,check=True,capture_output=True)
    (project.root/'cart.js').write_text('Dirty original')
    with pytest.raises(ValueError,match='uncommitted'):
        Workspace(settings.state_dir/'tasks'/'write',project).prepare(True)
    Workspace(settings.state_dir/'tasks'/'read',project).prepare(False)
