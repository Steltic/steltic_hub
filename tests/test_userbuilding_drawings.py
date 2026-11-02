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
    # Authored pilot answers must not supply invented data to an ordinary job.
    assert client.get(base).json()['available_test_scenario'] is None
    fictional=client.post(base+'/fictional-design',json={'revision':state['revision'],
        'source_model_sha256':state['source_model_sha256'],'confirm_fictional':True,
        'author':'Integration test'})
    assert fictional.status_code==422
    unchanged=client.get(base).json()
    assert unchanged['revision']==state['revision'] and unchanged['drawing_status']=='provisional'
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


@pytest.mark.parametrize('case,family',[('cfs26','review_canopy_knee'),('cfs27','review_next_portal_joint')])
def test_varied_case_uses_existing_queries_and_publishes_assembled_details(case,family):
    from steltic_openbim.review_cases import evidence
    from steltic_openbim.drawing_workflow import fingerprint
    package=evidence(case)['package'];digest=fingerprint(package)
    client=TestClient(um.app);project='DrawingReview'+uuid.uuid4().hex[:12]
    base=f'/api/project/{project}/drawings'
    response=client.post(base+'/upload',files={'file':('package.json',json.dumps(package))})
    assert response.status_code==200,response.text
    provisional=response.json()
    assert provisional['drawing_status']=='provisional'
    assert client.get(base).json()['available_test_scenario']==case
    assert not client.get(base).json()['questions']['blocks_drawings']
    payload=dict(revision=provisional['revision'],source_model_sha256=digest,confirm_fictional=True,author='Fictional test acceptance')
    adopted=client.post(base+'/fictional-design',json=payload)
    assert adopted.status_code==200,adopted.text
    assert adopted.json()['revision']==2
    scenes=client.get(base+'/file/connection_assemblies.json')
    assert scenes.status_code==200
    assert scenes.json()['source_sha256']==digest
    assert any(s['family']==family for s in scenes.json()['assemblies'])
    assert client.get(base+'/file/drawings.pdf').content.startswith(b'%PDF')
    assert client.post(base+'/fictional-design',json=payload).status_code==409
    current=client.get(base).json();request=current['requests'][0]
    reopened=client.post(base+'/answers',json=dict(revision=current['revision'],source_model_sha256=digest,id=request['id'],action='reopen',author='Fictional test acceptance',note='Reopen for coordination review.'))
    assert reopened.status_code==200,reopened.text
    assert reopened.json()['drawing_status']=='provisional'
    assert len(client.get(base+'/history').json()['revisions'])==3
    assert fingerprint(package)==digest
