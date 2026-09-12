"""Small documented Geofabrik extraction-domain evidence, separate from mapping completeness."""
from pathlib import Path

from shapely.geometry import Polygon
from shapely.ops import unary_union

from seoul_visibility.acquisition_safety import guarded_download, sha256


def parse_poly(text):
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(lines)>20000 or lines[0].startswith('<'):
        raise ValueError('Invalid or oversized Geofabrik polygon text')
    index=1;outer=[];holes=[];finished=False
    while index<len(lines):
        name=lines[index];index+=1
        if name=='END':
            finished=True;break
        coords=[]
        while index<len(lines) and lines[index]!='END':
            fields=lines[index].split();index+=1
            if len(fields)!=2:raise ValueError('Invalid polygon coordinate pair')
            x,y=map(float,fields)
            if not (-180<=x<=180 and -90<=y<=90):raise ValueError('Unexplained polygon coordinates')
            coords.append((x,y))
        if index>=len(lines) or len(coords)<4 or coords[0]!=coords[-1]:
            raise ValueError('Unclosed or truncated polygon ring')
        index+=1
        polygon=Polygon(coords)
        if polygon.is_empty or not polygon.is_valid:raise ValueError('Invalid polygon ring')
        (holes if name.startswith('!') else outer).append(polygon)
    if not finished or index!=len(lines) or not outer:raise ValueError('Truncated polygon sections')
    geometry=unary_union(outer)
    for hole in holes:
        if not geometry.covers(hole):raise ValueError('Polygon hole is outside outer rings')
        geometry=geometry.difference(hole)
    if geometry.is_empty or not geometry.is_valid:raise ValueError('Invalid extraction polygon')
    return geometry


def acquire_distribution_geometry(raw, budget, geometries):
    path=Path(raw)/'south-korea.poly'
    url='https://download.geofabrik.de/asia/south-korea.poly'
    transfer=guarded_download(url,path,budget,max_bytes=128*1024,
        allowed_hosts={'download.geofabrik.de'},max_retries=2,
        validator=lambda p:parse_poly(p.read_text(encoding='utf-8')))
    geometry=parse_poly(path.read_text(encoding='utf-8'))
    coverage={role:{'covered':geometry.covers(area),
                    'uncovered_area_degrees2':area.difference(geometry).area}
              for role,area in geometries.items()}
    return {'path':str(path),'bytes':path.stat().st_size,'sha256':sha256(path),
            'url':url,'transfer':transfer,'crs':'EPSG:4326','geometry_type':geometry.geom_type,
            'bounds_lonlat':list(geometry.bounds),'requested_geometry_checks':coverage,
            'independent_publisher_checksum_verified':False,
            'scope':'Publisher extraction polygon retrieved separately from the dated PBF; distribution-domain evidence only, not proof of complete mapped objects or relation geometry',
            'source_date':'Retrieval snapshot of current extraction domain; not an observation date or a dated PBF geometry-version guarantee'}
