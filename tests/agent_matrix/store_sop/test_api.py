import json
from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport
from agent_matrix.store_sop.api import create_app, API_PREFIX
from agent_matrix.store_sop.errors import SessionConflictError, ChatModelUnavailableError, StructuredOutputError
from data_platform.retrieval_engine.models import ToolErrorResponse
from data_platform.mcp_server.app import create_app as data_app


def headers(user):
    return {'X-User-Id':user.user_id,'X-Dept-Id':user.dept_id,'X-User-Roles':','.join(user.roles)}


def client(agent=None, **kwargs):
    return AsyncClient(transport=ASGITransport(app=create_app(agent,**kwargs),raise_app_exceptions=False),base_url='http://test')


def error(response,status,code):
    assert response.status_code==status,response.text
    assert ToolErrorResponse.model_validate(response.json()).error_code==code


def test_openapi_workflow_contract():
    schema=create_app().openapi()
    ops={op['operationId'] for path in schema['paths'].values() for op in path.values()}
    assert {'store_sop_chat_turn','store_sop_get_session','store_sop_end_session'}<=ops
    assert {'StoreAgentTurnPayload','StoreAgentTurnResponse','ToolErrorResponse'}<=schema['components']['schemas'].keys()
    workflow=Path('agent_matrix/workflows/store_sop_dify_workflow.yml').read_text()
    assert 'store_sop_chat_turn' in workflow
    assert 'search_knowledge_base' in workflow
    assert 'search_knowledge_base' in json.dumps(data_app().openapi())


async def test_gateway_identity_and_sessions(agent,user,monkeypatch):
    captured=[]
    original=agent.handle_turn
    async def spy(req):
        captured.append(req)
        return await original(req)
    monkeypatch.setattr(agent,'handle_turn',spy)
    async with client(agent) as http:
        response=await http.post(API_PREFIX+'/turns',headers=headers(user),json={
            'message':'制冰机坏了','user_context':{'user_id':'attacker','dept_id':'finance','roles':['finance']}})
        assert response.status_code==200,response.text
        assert captured[0].user_context==user
        session=response.json()['session_id']
        response=await http.get(API_PREFIX+'/sessions/'+session,headers=headers(user))
        assert response.status_code==200 and response.json()['turn_count']==1
        forbidden=headers(user)|{'X-User-Id':'another'}
        error(await http.get(API_PREFIX+'/sessions/'+session,headers=forbidden),403,'SESSION_FORBIDDEN')
        error(await http.delete(API_PREFIX+'/sessions/'+session,headers=forbidden),403,'SESSION_FORBIDDEN')
        assert (await http.delete(API_PREFIX+'/sessions/'+session,headers=headers(user))).status_code==204
        assert (await http.delete(API_PREFIX+'/sessions/'+session,headers=headers(user))).content==b''
        error(await http.get(API_PREFIX+'/sessions/'+session,headers=headers(user)),404,'SESSION_NOT_FOUND')


@pytest.mark.parametrize('identity',[{}, {'X-User-Id':'u'}, {'X-User-Id':'u','X-Dept-Id':'d','X-User-Roles':''},
    {'X-User-Id':'u','X-Dept-Id':'d','X-User-Roles':'clerk, '}, {'X-User-Id':'','X-Dept-Id':'d','X-User-Roles':'clerk'}])
async def test_unauthenticated(agent,user,identity):
    async with client(agent) as http:
        error(await http.post(API_PREFIX+'/turns',headers=identity,json={'message':'你好','user_context':user.model_dump()}),401,'UNAUTHENTICATED')


async def test_body_identity_opt_in(agent,user):
    async with client(agent,allow_body_identity=True) as http:
        assert (await http.post(API_PREFIX+'/turns',json={'message':'你好','user_context':user.model_dump()})).status_code==200
        error(await http.post(API_PREFIX+'/turns',headers={'X-User-Id':'u'},json={'message':'你好','user_context':user.model_dump()}),401,'UNAUTHENTICATED')


async def test_422_uninitialized_and_missing(agent,user):
    async with client() as http:
        error(await http.post(API_PREFIX+'/turns',headers=headers(user),json={'message':'你好'}),503,'AGENT_NOT_INITIALIZED')
    async with client(agent) as http:
        error(await http.post(API_PREFIX+'/turns',headers=headers(user),json={'message':''}),422,'INVALID_ARGUMENT')
        error(await http.post(API_PREFIX+'/turns',headers=headers(user),json={'message':'不进水','session_id':'missing'}),404,'SESSION_NOT_FOUND')


@pytest.mark.parametrize('exc,status,code',[
    (SessionConflictError('conflict'),409,'SESSION_CONFLICT'),
    (ChatModelUnavailableError('down'),503,'CHAT_MODEL_UNAVAILABLE'),
    (StructuredOutputError('invalid'),502,'STRUCTURED_OUTPUT_INVALID'),
    (RuntimeError('boom /secret/path'),503,'INTERNAL_ERROR'),
])
async def test_errors_sanitized(agent,user,monkeypatch,exc,status,code):
    async def fail(req): raise exc
    monkeypatch.setattr(agent,'handle_turn',fail)
    async with client(agent) as http:
        response=await http.post(API_PREFIX+'/turns',headers=headers(user),json={'message':'你好'})
        error(response,status,code)
        assert not any(s in response.text for s in ['boom','/secret/path','Traceback'])


async def test_retrieval_unavailable_is_200(agent,user,sop_service):
    sop_service.vector_search.fail=sop_service.keyword_search.fail=RuntimeError('offline')
    async with client(agent) as http:
        response=await http.post(API_PREFIX+'/turns',headers=headers(user),json={'message':'鲜柠檬茶柠檬片多少克'})
        assert response.status_code==200
        assert response.json()['stop_reason']=='retrieval_unavailable'
