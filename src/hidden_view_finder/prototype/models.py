"""Distance-only interface and the single evidence object used throughout the app."""
from __future__ import annotations
from dataclasses import dataclass,asdict,field
from datetime import datetime
from typing import TypedDict,Literal
from hidden_view_finder.models import aware_datetime,finite_number,RequestError
CATEGORIES={'mountain','city','river','greenery','skyline'}
@dataclass(frozen=True)
class Query:
 lon:float
 lat:float
 radius_m:int
 view_at:datetime
 preferences:tuple[str,...]
 composition:str='any'
 crowd_preference:str='any'
 limit:int=3
 @classmethod
 def parse(cls,data):
  if not isinstance(data,dict) or set(data)-{'origin','radius_m','view_at','preferences','composition','crowd_preference','limit'}: raise RequestError('Unknown request fields')
  o=data.get('origin',{})
  if set(o)-{'lon','lat'}: raise RequestError('Origin contains unknown fields')
  lon=finite_number(o.get('lon'),'origin.lon',-180,180);lat=finite_number(o.get('lat'),'origin.lat',-90,90)
  radius=finite_number(data.get('radius_m',3000),'radius_m',100,10000)
  if 'view_at' not in data: raise RequestError('view_at is intended viewing time in Asia/Seoul')
  at=aware_datetime(data['view_at'],'Asia/Seoul','view_at')
  if not 2000<=at.year<=2100: raise RequestError('view_at year outside solar-model interface bounds')
  prefs=data.get('preferences',['mountain','city'])
  if not isinstance(prefs,list) or len(prefs)>5 or not all(isinstance(x,str) and x in CATEGORIES for x in prefs): raise RequestError('Invalid scenery preferences')
  composition=data.get('composition','any');crowd=data.get('crowd_preference','any')
  if composition not in {'open','framed','any'} or crowd not in {'quiet','any'}: raise RequestError('Invalid composition/crowd preference')
  n=finite_number(data.get('limit',3),'limit',1,3)
  if not n.is_integer(): raise RequestError('limit must be an integer')
  return cls(lon,lat,int(radius),at,tuple(dict.fromkeys(prefs)),composition,crowd,int(n))

class Sample(TypedDict):
 evidence_id:str
 target_id:str
 category:str
 name:str
 bearing_deg:float
 distance_m:float
 state:Literal['visible','blocked','unknown','excluded']
 target:dict
 angular_elevation_deg:float|None

@dataclass
class SceneEvidence:
 view_id:str
 candidate_id:str
 name:str
 standing:dict
 orientation:dict
 view_at:str
 proximity_m:float
 scene_samples:list[Sample]
 supported_categories:list[str]
 coverage:dict
 access:dict
 versions:dict
 solar:dict
 work:dict
 limitations:list[str]
 score:dict=field(default_factory=dict)
 description:str=''
 composition:dict=field(default_factory=dict)
 preview:dict=field(default_factory=dict)
 route_distance_m:None=None
 estimated_travel_minutes:None=None
 field_verified:bool=False
 description_method:str='deterministic_template'
 weather:dict=field(default_factory=lambda:{'status':'unknown','atmospheric_visibility':'unknown'})
 crowd:dict=field(default_factory=lambda:{'status':'unknown','scope':None})
 def to_dict(self): return asdict(self)
