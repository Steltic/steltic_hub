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
    assert [t['title'] for t in drawings['tabs']]==['Steltic import','IFC import','IFC export','Files']
    assert drawings['tabs'][1]['src']=='/ifc-import?project={job}'
    assert drawings['tabs'][2]['src']=='/ifc-export?project={job}'
    assert drawings['server']['requires']==['steltic','steltic_cfs']
    assert drawings['env_vars']['HR_URL']=='{server.steltic}'
    assert drawings['env_vars']['CFS_URL']=='{server.steltic_cfs}'
    assert user['server']['requires']==['steltic','steltic_cfs']
    assert user['env_vars']['CFS_URL']=='{server.steltic_cfs}'


def test_ordinary_hr_grouped_export_recovers_occurrences_and_questions():
    from steltic_openbim.drawing_workflow import fingerprint
    package=json.loads((ROOT/'tests/fixtures/drawings/synthetic_bim_package.json').read_text())
    member=package['physical']['members'][0];a,b=member['axis']
    package['module']='HR';package['layout_evidence']={'levels_in':[0,max(a[2],b[2])]}
    package['analysis']={'nodes':[{'id':key,'point':point} for key,point in
        [('a',a),('b',b),('c',[a[0],a[1]+240,a[2]]),('d',[b[0],b[1]+240,b[2]])]],
        'elements':[{'id':'e1','node_ids':['a','b'],'kind':'beam','profile_id':member['profile_id']},
                    {'id':'e2','node_ids':['c','d'],'kind':'beam','profile_id':member['profile_id']}]}
    member['analysis_ids']=['e1','e2'];package['readiness']={'status':'ready'}
    digest=fingerprint(package);client=TestClient(um.app)
    base='/api/project/Recovery'+uuid.uuid4().hex[:12]+'/drawings'
    response=client.post(base+'/upload',files={'file':('package.json',json.dumps(package))})
    assert response.status_code==200,response.text
    state=response.json()
    assert state['representation']=='captured_source_worklines'
    assert state['drawing_counts']['drawn_occurrences']==2
    assert state['source_readiness']=='needs_input' and state['drawing_status']=='provisional'
    assert {'MODEL-OCCURRENCES','MODEL-CONNECTIONS','MODEL-FLOORS','MODEL-OPENINGS'}<={r['id'] for r in state['requests']}
    assert all(r['accepted'] and r['proposal'] for r in state['requests'])
    current=client.get(base).json()
    assert current['available_test_scenario'] is None and not current['questions']['blocks_drawings']
    model=client.get(base+'/file/source_workline_model.json').json()
    assert {m['id'] for m in model['members']}=={'e1','e2'}
    new=client.post(base+'/regenerate',json={'revision':state['revision'],'source_model_sha256':digest})
    assert new.status_code==200 and new.json()['revision']==2
    assert fingerprint(package)==digest


@pytest.mark.parametrize('case,family',[
    ('cfs26','review_canopy_knee'),('cfs27','review_next_portal_joint'),
    ('hr19','review_pg_seat'),
])
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
