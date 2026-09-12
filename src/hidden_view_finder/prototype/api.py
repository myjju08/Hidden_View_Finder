"""Local FastAPI boundary, bounded worker and payloads; no source files exposed."""
from __future__ import annotations
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict,deque
from pathlib import Path
import asyncio,json,time,math,re,uuid,threading,hashlib,sqlite3,secrets
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import JSONResponse,FileResponse
from pydantic import BaseModel,ConfigDict,Field
from .runtime import config,budget,public_storage,ROOT
from .models import Query as DiscoveryQuery
from .data import Data,TO_LL
from .scenes import Discovery
from .ai import PrototypeAI,ImageJobs
from seoul_visibility.errors import ResourceBudgetError

SESSION_COOKIE='hvf_session'
SESSION_TTL_SECONDS=3600
MAX_SESSIONS=256


def _bounded_metadata(path, maximum=2_000_000):
 path=Path(path)
 if path.is_symlink() or path.stat().st_size>maximum:raise ValueError('Metadata file exceeds verified local contract')
 blob=path.read_bytes()
 if len(blob)>maximum:raise ValueError('Metadata grew beyond bound')
 value=json.loads(blob)
 if not isinstance(value,dict):raise ValueError('Metadata must be an object')
 return value,hashlib.sha256(blob).hexdigest()

def public_input_metadata(package_path,tile_path):
 """Small allowlisted metadata, with dates and fractions derived from inputs.

 The owning geometry worker independently validates GIS files. This function
 additionally verifies the published source receipt's hash before displaying
 its dates/licences. It never emits paths, raw source records or cleanup logs.
 """
 package,package_hash=_bounded_metadata(package_path)
 tiles,tile_hash=_bounded_metadata(tile_path)
 sources={};source_hash=None
 artifact=next((item for item in package.get('artifacts',[]) if item.get('path')=='sources.json'),None)
 if artifact:
  source_path=Path(package_path).parent/'sources.json'
  sources,source_hash=_bounded_metadata(source_path)
  if source_hash!=artifact.get('sha256') or source_path.stat().st_size!=artifact.get('bytes'):
   raise ValueError('Source metadata does not match the published package')
 terrain=sources.get('terrain',{}).get('result',{})
 osm=sources.get('osm',{}).get('result',{})
 buildings=sources.get('buildings',{}).get('result',{})
 counts=tiles.get('cell_counts',{})
 def fraction(numerator,denominator):
  a,b=counts.get(numerator),counts.get(denominator)
  return a/b if type(a) is int and type(b) is int and 0<=a<=b and b>0 else None
 def fields(record,names):
  return {key:record[key] for key in names if key in record and isinstance(record[key],(str,int,float,bool,list))}
 return {'readiness':package.get('readiness',{}),'data':{
  'package_id':package.get('package_id'),'terrain_source_year':terrain.get('source_year'),
  'building_imagery_years':buildings.get('source_date'),'osm_snapshot':osm.get('source_timestamp'),
  'tile_resolution_m':tiles.get('resolution_m'),'tile_count':len(tiles.get('tiles',[])),
  'terrain_seoul_fraction':fraction('terrain_valid_seoul_cells','inside_seoul_cells'),
  'terrain_support_fraction':fraction('terrain_valid_support_cells','requested_support_cells'),
  'terrain_cell_counts':{key:counts.get(key) for key in ('inside_seoul_cells','requested_support_cells','terrain_valid_seoul_cells','terrain_valid_support_cells')},
  'obstruction_quality_counts':fields(tiles.get('surface_quality_counts',{}),('building_coverage_valid','height_estimated','height_unresolved','roof_conflict')),
  'coverage_measurement':tiles.get('coverage_measurement'),'coverage_is_visibility_probability':False,
  'sources':{'terrain':fields(terrain,('source_year','source_file_updated','catalogue','licence','vertical_reference')),
             'osm':fields(osm,('source_timestamp','catalogue','licence','attribution')),
             'buildings':fields(buildings,('source_date','source_catalogue','height_reference','height_units','height_estimated','licences','deployment_licence_review'))},
  'metadata_hashes':{'package':package_hash,'tile_collection':tile_hash,'source_receipts':source_hash},
  'metadata_status':'validated_source_receipt' if artifact else 'source_receipt_unavailable',
  'field_verified':False}}

class Origin(BaseModel):
 model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
 lon:float=Field(ge=-180,le=180,strict=True)
 lat:float=Field(ge=-90,le=90,strict=True)
class RecommendationBody(BaseModel):
 model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
 origin:Origin
 radius_m:int=Field(default=3000,ge=100,le=10000,strict=True)
 view_at:str=Field(max_length=64)
 preferences:list[str]=Field(default=['mountain','city'],max_length=5)
 composition:str=Field(default='any',max_length=16)
 crowd_preference:str=Field(default='any',max_length=16)
 limit:int=Field(default=3,ge=1,le=3,strict=True)
class ImageBody(BaseModel):
 model_config=ConfigDict(extra='forbid')
 view_id:str=Field(pattern=r'^[a-f0-9]{24}$')

class Service:
 def __init__(self,c):
  self.config=c;self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='hvf-geometry');self.geometry=None;self.busy=False;self.views=OrderedDict();self.init_error=None;self.started=time.monotonic();self.map_slots=threading.BoundedSemaphore(2)
  self.provider=PrototypeAI(c['ai'],budget(c),Path(c['runtime_root']));self.jobs=ImageJobs(self.provider);self.rates=OrderedDict();self.sessions=OrderedDict();self.input_metadata={'readiness':{},'data':{'metadata_status':'unavailable','field_verified':False}}
 def initialize(self):
  try:self.input_metadata=public_input_metadata(self.config['package_manifest'],self.config['tile_manifest'])
  except (ValueError,OSError,KeyError,TypeError) as e:self.init_error=type(e).__name__
  try:self.geometry=Discovery(self.config)
  except (ValueError,OSError,RuntimeError,sqlite3.Error) as e:self.init_error=type(e).__name__
 def allowed(self,ip,expensive=False):
  now=time.monotonic();key=(ip,expensive);events=self.rates.get(key,deque());window=60;maximum=10 if expensive else 180
  while events and events[0]<now-window: events.popleft()
  if len(events)>=maximum:return False
  events.append(now);self.rates[key]=events;self.rates.move_to_end(key)
  while len(self.rates)>1024:self.rates.popitem(last=False)
  return True
 def session(self,cookie):
  """Anonymous bearer cookie; registry retains only its hash and issue time.

  Called by the single ASGI event loop. Unknown client-chosen values are never
  accepted as a session, preventing session fixation by fabricated identifiers.
  Expiry and registry eviction also discard associated request context.
  """
  now=time.monotonic()
  expired={key for key,issued in self.sessions.items() if now-issued>=SESSION_TTL_SECONDS}
  for key in expired:self.sessions.pop(key,None)
  if expired:self.views=OrderedDict((key,value) for key,value in self.views.items() if key[0] not in expired)
  identity=hashlib.sha256(cookie.encode()).hexdigest() if isinstance(cookie,str) and re.fullmatch(r'[A-Za-z0-9_-]{43}',cookie) else None
  if identity in self.sessions:
   return identity,None
  token=secrets.token_urlsafe(32);identity=hashlib.sha256(token.encode()).hexdigest()
  self.sessions[identity]=now
  while len(self.sessions)>MAX_SESSIONS:
   retired,_=self.sessions.popitem(last=False)
   self.views=OrderedDict((key,value) for key,value in self.views.items() if key[0]!=retired)
  return identity,token
 def remember(self,session,scenes):
  if session not in self.sessions:return {}
  retained={}
  for scene in scenes:
   # Origins are never stored; origin-dependent distance/rank stays private to
   # this anonymous session. The view's geographic identity remains unchanged.
   key=(session,scene['view_id']);self.views[key]=scene;self.views.move_to_end(key);retained[scene['view_id']]=scene
  while len(self.views)>self.config['limits']['memory_scene_entries']:self.views.popitem(last=False)
  return retained
 def close_worker(self):
  if self.geometry:self.geometry.close()


def create_app(config_path=None):
 c=config(config_path);service=Service(c)
 @asynccontextmanager
 async def lifespan(app):
  # All GDAL resources are opened/used/closed by this one owning worker thread.
  await asyncio.get_running_loop().run_in_executor(service.executor,service.initialize)
  yield
  await asyncio.get_running_loop().run_in_executor(service.executor,service.close_worker)
  service.executor.shutdown(wait=True,cancel_futures=True);service.jobs.close()
 app=FastAPI(title='Hidden View Finder local prototype',docs_url=None,redoc_url=None,lifespan=lifespan)
 app.state.service=service
 @app.middleware('http')
 async def controls(request,call_next):
  path=request.url.path;origin=request.headers.get('origin')
  allowed_origins={f'http://127.0.0.1:{request.url.port or 80}',f'http://localhost:{request.url.port or 80}'}
  if origin and origin not in allowed_origins:return JSONResponse({'code':'origin_not_allowed'},403)
  if request.headers.get('host','').split(':')[0] not in {'127.0.0.1','localhost','testserver'}:return JSONResponse({'code':'host_not_allowed'},403)
  if len(str(request.url))>4096:return JSONResponse({'code':'request_too_large'},414)
  if request.method=='POST':
   try: length=int(request.headers.get('content-length','0'))
   except ValueError:length=0
   if not 0<length<=8192:return JSONResponse({'code':'bounded_body_required'},413)
   if request.headers.get('content-type','').split(';')[0]!='application/json':return JSONResponse({'code':'json_required'},415)
   body=bytearray()
   try:
    async with asyncio.timeout(5):
     async for chunk in request.stream():
      if len(body)+len(chunk)>min(length,8192):return JSONResponse({'code':'request_too_large'},413)
      body.extend(chunk)
   except TimeoutError:return JSONResponse({'code':'body_read_timeout'},408)
   if len(body)!=length:return JSONResponse({'code':'body_length_mismatch'},400)
   request._body=bytes(body)
  ip=request.client.host if request.client else 'unknown'
  if not service.allowed(ip,request.method=='POST'):return JSONResponse({'code':'rate_limited','retry_after_s':60},429,headers={'Retry-After':'60'})
  request.state.session,new_cookie=service.session(request.cookies.get(SESSION_COOKIE))
  try: response=await call_next(request)
  except ResourceBudgetError:return JSONResponse({'code':'storage_blocked','message':'New writes refused; read-only map remains available.'},507)
  except Exception:return JSONResponse({'code':'request_failed','message':'The request could not be evaluated. No unsupported result was substituted.'},500)
  response.headers['X-Content-Type-Options']='nosniff';response.headers['Referrer-Policy']='no-referrer';response.headers['Cache-Control']='no-store' if path.startswith('/api/') else 'no-cache'
  response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
  if new_cookie:response.set_cookie(SESSION_COOKIE,new_cookie,max_age=SESSION_TTL_SECONDS,httponly=True,samesite='strict',secure=request.url.scheme=='https',path='/')
  return response
 @app.get('/api/health')
 async def health():return {'status':'ok' if service.geometry else 'data_unavailable','geometry_worker':'one','uptime_seconds':round(time.monotonic()-service.started,1)}
 @app.get('/api/capabilities')
 async def capabilities():
  try:storage=public_storage(c)
  except ResourceBudgetError:storage={'status':'storage_blocked'}
  return {'local_exploration_available':service.geometry is not None,'supported_query_scope':'individually validated sparse rays over read-only tile collection; global completeness is not assumed','source_readiness':service.input_metadata['readiness'],'geographic_ready':False,'visibility_ready':False,'public_deployment_status':c['public_deployment_status'],'provider_status':service.provider.status(),'data':service.input_metadata['data'],'limits':c['limits'],'storage':storage,'notice':'실험용 · 데이터 범위 불완전 · 지도/모델 추정 · 현장 미검증','routing':'직선거리만 사용 · 경로 및 이동시간 미확인','map_center':{'lon':126.978,'lat':37.5665,'zoom':11},'data_issue':service.init_error}
 @app.get('/api/places')
 def places(q:str=''):
  if len(q)>80:raise HTTPException(422,'Search text too long')
  if not service.map_slots.acquire(blocking=False):raise HTTPException(429,'Map/search workers busy')
  d=None
  try:
   d=Data(c['package_manifest']);return {'places':d.places(q)}
  finally:
   if d:d.close()
   service.map_slots.release()
 @app.get('/api/map')
 def map_features(bbox:str='126.75,37.42,127.2,37.7',zoom:float=11):
  try:box=[float(v) for v in bbox.split(',')]
  except ValueError:raise HTTPException(422,'Invalid bbox')
  if len(box)!=4 or not all(math.isfinite(x) for x in box) or not 124<=box[0]<box[2]<=130 or not 32<=box[1]<box[3]<=40 or box[2]-box[0]>3 or box[3]-box[1]>3 or not 8<=zoom<=19:raise HTTPException(422,'Bounded Korea viewport required')
  if not service.map_slots.acquire(blocking=False):raise HTTPException(429,'Map/search workers busy')
  d=None
  try:
   d=Data(c['package_manifest']);return d.map(box,zoom,c['limits']['map_features'])
  finally:
   if d:d.close()
   service.map_slots.release()
 @app.get('/api/coverage')
 def coverage():
  manifest=Path(c['tile_manifest']);m=json.loads(manifest.read_text());features=[]
  for entry in m['tiles']:
   p=manifest.parent/entry['path'];tile=json.loads(p.read_text());b=tile['grid']['bounds']
   if not b:continue
   xy=[(b[0],b[1]),(b[2],b[1]),(b[2],b[3]),(b[0],b[3]),(b[0],b[1])]
   counts=tile.get('cell_counts',{});fraction=counts.get('terrain_valid_support_cells',0)/max(1,counts.get('requested_support_cells',1000000))
   features.append({'type':'Feature','geometry':{'type':'Polygon','coordinates':[[list(TO_LL.transform(x,y)) for x,y in xy]]},'properties':{'layer':'coverage','tile_id':entry['id'],'terrain_fraction':fraction,'resolution_m':5,'quality':'tile_aggregate_not_ray_support','supported_query_requires_per_ray_checks':True}})
  return {'type':'FeatureCollection','features':features,'notice':'Coarse tile aggregates; individual NoData cells and every ray are checked at query time.'}
 @app.post('/api/recommendations')
 async def recommendations(body:RecommendationBody,request:Request):
  try:q=DiscoveryQuery.parse(body.model_dump())
  except ValueError as e:raise HTTPException(422,str(e))
  if not service.geometry: return JSONResponse({'code':'data_unavailable','status':'empty','views':[],'message':'Local package/tile validation did not pass; source files remain unchanged.'},409)
  if service.busy:return JSONResponse({'code':'geometry_busy','retry_after_s':5},429,headers={'Retry-After':'5'})
  service.busy=True
  loop=asyncio.get_running_loop()
  # Tie ownership to the actual concurrent worker, never the cancelled ASGI task.
  try: worker=service.executor.submit(service.geometry.recommend,q)
  except Exception:
   service.busy=False;raise
  worker.add_done_callback(lambda _:loop.call_soon_threadsafe(setattr,service,'busy',False))
  result=await asyncio.shield(asyncio.wrap_future(worker))
  retained=service.remember(request.state.session,result.pop('_all_scenes',[]));result['request_id']=uuid.uuid4().hex
  comparison=await loop.run_in_executor(None,service.provider.compare,result['views'])
  result['presentation']=comparison
  for annotation in comparison.get('annotations',[]):
   identity=annotation.get('view_id')
   key=(request.state.session,identity)
   if key in service.views and service.views[key] is retained.get(identity):service.views[key]['ai_presentation']=annotation
   for view in result['views']:
    if view['view_id']==identity:view['ai_presentation']=annotation
  return result
 @app.get('/api/views/{view_id}')
 async def view(view_id:str,request:Request):
  key=(request.state.session,view_id)
  if not re.fullmatch('[a-f0-9]{24}',view_id) or key not in service.views:raise HTTPException(404,'View expired or unavailable in this session; repeat the bounded query')
  return service.views[key]
 @app.post('/api/images')
 async def images(body:ImageBody,request:Request):
  key=(request.state.session,body.view_id)
  if key not in service.views:raise HTTPException(404,'View expired or unavailable in this session')
  return service.jobs.submit(service.views[key])
 @app.get('/api/images/{key}')
 async def image_status(key:str):
  if re.fullmatch(r'[a-f0-9]{64}\.(?:thumb\.)?jpg',key):
   identity=key.split('.')[0];path=service.provider.cached_image(identity,thumbnail='.thumb.' in key)
   if not path:raise HTTPException(404)
   return FileResponse(path,media_type='image/jpeg')
  if not re.fullmatch('[a-f0-9]{64}',key):raise HTTPException(422,'Invalid job key')
  return service.jobs.status(key)
 @app.get('/api/image-assets/{key}/{size}')
 async def image_asset(key:str,size:str):
  if not re.fullmatch('[a-f0-9]{64}',key) or size not in {'display','thumbnail'}:raise HTTPException(404)
  path=service.provider.cached_image(key,thumbnail=size=='thumbnail')
  if not path:raise HTTPException(404)
  return FileResponse(path,media_type='image/jpeg')
 static=ROOT/'src/hidden_view_finder/static'
 @app.get('/')
 async def home():return FileResponse(static/'prototype.html')
 @app.get('/static/{name:path}')
 async def asset(name:str):
  # Only bundled prototype code/renderer. Raw sources/manifests/demo art excluded.
  allowed={'prototype.js','prototype.css','vendor/leaflet/leaflet.js','vendor/leaflet/leaflet.css','vendor/fonts/NotoSansKR.ttf'}
  if name not in allowed:raise HTTPException(404)
  return FileResponse(static/name)
 return app
