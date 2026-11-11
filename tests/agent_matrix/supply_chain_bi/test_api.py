import json
from types import SimpleNamespace
import httpx
import pytest
from agent_matrix.supply_chain_bi.api import create_app, API_PREFIX
from agent_matrix.supply_chain_bi.errors import StructuredOutputError, SqlModelUnavailableError
from data_platform.retrieval_engine.models import ToolErrorResponse
from .fakes import *

HEADERS={'X-User-Id':'mgr_east','X-Dept-Id':'supply_chain','X-User-Roles':'supply_manager'}

def test_39_openapi():
    schema=create_app().openapi()
    assert set(schema['paths'])=={API_PREFIX+'/analyze',API_PREFIX+'/catalog'}
    assert schema['paths'][API_PREFIX+'/analyze']['post']['operationId']=='supply_chain_bi_analyze'
    assert schema['paths'][API_PREFIX+'/catalog']['get']['operationId']=='supply_chain_bi_get_catalog'
    assert {'SupplyChainQueryPayload','SupplyChainQueryResponse','InventoryAlert','ToolErrorResponse'}<=set(schema['components']['schemas'])
    assert not any('sql' in path for path in schema['paths'])

async def call(app,body=None,headers=None,path='/analyze',method='POST'):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://test') as client:
        return await client.request(method,API_PREFIX+path,json=body,headers=headers)

async def test_40_gateway_override(make_agent):
    agent=make_agent();app=create_app(agent)
    r=await call(app,{'question':'损耗率','user_context':user('hq_analyst').model_dump()},HEADERS)
    assert r.status_code==200,r.text
    assert agent.scope_resolver.calls[0]==user('mgr_east')
    r=await call(app,{'question':'损耗率','user_context':user().model_dump()})
    assert r.status_code==401
    r=await call(app,{'question':'损耗率','region_id':'R_SOUTH'},HEADERS)
    assert r.status_code==422
    r=await call(create_app(agent,allow_body_identity=True),{'question':'损耗率','user_context':user().model_dump()})
    assert r.status_code==200

@pytest.mark.parametrize('headers',[{}, {'X-User-Id':'mgr_east'}, HEADERS|{'X-User-Roles':' , '}])
async def test_41_unauthenticated(make_agent,headers):
    r=await call(create_app(make_agent()),{'question':'损耗率'},headers)
    assert r.status_code==401
    assert ToolErrorResponse.model_validate(r.json()).error_code=='UNAUTHENTICATED'

@pytest.mark.parametrize('kind,status,code',[('forbidden',403,'SCOPE_FORBIDDEN'),('invalid',422,'INVALID_ARGUMENT'),('structured',502,'STRUCTURED_OUTPUT_INVALID'),('model',503,'SQL_MODEL_UNAVAILABLE'),('missing',503,'AGENT_NOT_INITIALIZED'),('scope',503,'SCOPE_RESOLUTION_FAILED')])
async def test_41_structured_errors(make_agent,kind,status,code):
    kwargs={};headers=HEADERS;body={'question':'损耗率'}
    if kind=='forbidden':headers={'X-User-Id':'clerk_01','X-Dept-Id':'store_ops','X-User-Roles':'clerk'}
    if kind=='invalid':body={'question':''}
    if kind=='structured':kwargs['model']=ScriptedSqlModel(fail=StructuredOutputError('schema invalid'))
    if kind=='model':kwargs['model']=ScriptedSqlModel(fail=RuntimeError('private source'))
    if kind=='scope':kwargs['resolver']=StaticScopeResolver(fail=RuntimeError('private source'))
    r=await call(create_app(None if kind=='missing' else make_agent(**kwargs)),body,headers)
    assert r.status_code==status,r.text
    assert ToolErrorResponse.model_validate(r.json()).error_code==code

async def test_42_unexpected_redacted(make_agent,caplog):
    async def boom(request):raise RuntimeError('boom /secret/path SELECT supplier_cost_price')
    agent=make_agent();agent.analyze=boom
    r=await call(create_app(agent),{'question':'损耗率'},HEADERS)
    assert r.status_code==503
    assert ToolErrorResponse.model_validate(r.json()).error_code=='INTERNAL_ERROR'
    for secret in ('boom','/secret/path','supplier_cost_price','Traceback'):
        assert secret not in r.text+caplog.text

async def test_43_catalog(make_agent):
    r=await call(create_app(make_agent()),headers=HEADERS,path='/catalog',method='GET')
    assert r.status_code==200
    for secret in SemanticLayer().catalog.global_sensitive_columns+SemanticLayer().catalog.forbidden_tables:
        assert secret not in r.text
