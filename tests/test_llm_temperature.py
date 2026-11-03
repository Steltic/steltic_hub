import importlib.util,json
from pathlib import Path
import httpx
import pytest


@pytest.fixture(params=['userbuilding','variations','admin'])
def llm(request,monkeypatch):
    p=Path(__file__).resolve().parents[1]/'steltic_hub/catalog'/('steltic_'+request.param)/request.param/'llm.py'
    spec=importlib.util.spec_from_file_location('test_llm_'+request.param,p)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.set_creds(dict(base_url='https://model.invalid',model='claude-test',api_key='test'))
    return module


def test_temperature_and_json_adapt_independently_and_cache(llm,monkeypatch):
    bodies=[];real_client=httpx.Client
    def serve(request):
        body=json.loads(request.content);bodies.append(body)
        if 'temperature' in body:return httpx.Response(400,json={'error':{'message':'`temperature` is deprecated for this model.'}})
        if 'response_format' in body:return httpx.Response(400,text='response_format unsupported')
        return httpx.Response(200,json={'choices':[{'message':{'content':'{}'}}]})
    monkeypatch.setattr(llm.httpx,'Client',lambda **kw:real_client(transport=httpx.MockTransport(serve)))
    assert llm.chat('system','user')=='{}'
    assert len(bodies)==3 and 'temperature' not in bodies[1] and 'response_format' in bodies[1]
    assert llm.chat('system','user',json_mode=False)=='{}'
    assert len(bodies)==4 and 'temperature' not in bodies[3]


def test_authentication_errors_are_not_retried(llm,monkeypatch):
    bodies=[];real_client=httpx.Client
    def serve(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(401,text='authentication required')
    monkeypatch.setattr(llm.httpx,'Client',lambda **kw:real_client(transport=httpx.MockTransport(serve)))
    with pytest.raises(httpx.HTTPStatusError):llm.chat('system','user',temperature=None)
    assert len(bodies)==1 and 'temperature' not in bodies[0]
