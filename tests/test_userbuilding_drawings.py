"""Integration with the optional openBIM module; install its [hub] extra to run."""
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

import pytest

pytest.importorskip('steltic_openbim.drawing_api')
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'steltic_hub/catalog/steltic_userbuilding'))
os.environ.setdefault('UB_JOBS',tempfile.mkdtemp(prefix='drawing-hub-test-'))
from userbuilding import main as um
from fastapi.testclient import TestClient


def test_existing_question_page_can_publish_without_resolving_information():
    client=TestClient(um.app)
    package=json.loads((ROOT/'tests/fixtures/drawings/synthetic_bim_package.json').read_text())
    package['review']={'status':'needs_input','recipe':'synthetic-integration','recipe_version':'1','items':[{'id':'ARCH-INFO','title':'Confirm architectural interface',
        'kind':'delegated','status':'coordination_required','basis':'Synthetic integration fixture.','entity_ids':[]}]}
    project='DrawingIntegration'+uuid.uuid4().hex[:12]
    base=f'/api/project/{project}/drawings'
    result=client.post(base+'/upload',files={'file':('package.json',json.dumps(package))})
    assert result.status_code==200,result.text
    state=result.json()
    assert state['can_generate'] and state['drawing_status']=='provisional'
    questions=client.get(base).json()['questions']
    assert questions['ready'] and not questions['blocks_drawings']
    assert questions['groups'][0]['items'][0]['accepted']
    before=client.get(base+'/file/drawings.pdf').content
    assert before.startswith(b'%PDF')
    updated=client.post(base+'/answers',json={'revision':state['revision'],
        'source_model_sha256':state['source_model_sha256'],'id':'ARCH-INFO',
        'action':'information','author':'Integration test','note':'Architect to provide the final interface.'})
    assert updated.status_code==200,updated.text
    assert updated.json()['revision']==2 and not updated.json()['requests'][0]['confirmed']
    assert len(client.get(base+'/history').json()['revisions'])==2
    assert 'StelticDrawingQueries' in client.get('/openbim-ui.js').text
    assert 'StelticDrawingQueries.mount' in client.get('/static/app.js').text
    # An architectural note must never bypass the existing nonlinear confirmation gate.
    assert client.get(f'/api/project/{project}/checks').status_code==409


def test_hub_and_openbim_manifest_share_the_project_store():
    cat=ROOT/'steltic_hub/catalog'
    user=json.loads((cat/'steltic_userbuilding/steltic_module.json').read_text(encoding='utf-8'))
    drawings=json.loads((cat/'steltic_openbim.json').read_text())
    assert user['env_vars']['OPENBIM_JOBS']=='{data_dir}/modules_data/steltic_openbim/sessions/local/jobs'
    assert drawings['env_vars']['OPENBIM_JOBS']=='{jobs_dir}'
    assert any(t.get('src','').endswith('&step=6') for t in user['tabs'])
