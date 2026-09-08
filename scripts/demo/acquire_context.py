#!/usr/bin/env python3
"""Acquire a bounded, cached OSM pedestrian extract for the real Namsan demo.

No API call occurs during recommendations. Original responses remain under
data/demo/raw; this script creates context.json, not a visibility dataset.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pyproj import Geod
from shapely.geometry import LineString, Point, Polygon, box, mapping
from shapely.ops import polygonize, unary_union

from seoul_visibility.resources import preflight

BBOX = [126.973, 37.538, 127.007, 37.566]
LIMIT = 5 * 1024**2
ENDPOINTS = ["https://overpass.private.coffee/api/interpreter",
             "https://overpass-api.de/api/interpreter"]
USER_AGENT = ("HiddenViewFinder/0.1 (+https://github.com/myjju08/Hidden_View_Finder; "
              "bounded Seoul research extract)")
QUERIES = {
    "osm_namsan": '''[out:json][timeout:30];(way[highway~"^(footway|path|steps|pedestrian|residential|living_street|service|unclassified|tertiary|secondary)$"](37.538,126.973,37.566,127.007);nwr[leisure=park](37.538,126.973,37.566,127.007);way[natural~"^(wood|water|scrub|grassland)$"](37.538,126.973,37.566,127.007);way[landuse~"^(forest|grass)$"](37.538,126.973,37.566,127.007);nwr["name:en"="N Seoul Tower"](37.548,126.985,37.554,126.992););out meta geom;''',
    "osm_supplement": '''[out:json][timeout:30];(relation(16474080);node[barrier](37.538,126.973,37.566,127.007);node[railway=subway_entrance](37.560,126.981,37.562,126.988););out meta geom;''',
    "osm_tower_parts": '''[out:json][timeout:20];(nwr["building:part"](37.5509,126.9878,37.55165,126.9887);nwr[man_made=tower](37.5509,126.9878,37.55165,126.9887););out meta geom;''',
}
META_NAMES = {"osm_namsan": "acquisition.json", "osm_supplement": "supplement_acquisition.json",
              "osm_tower_parts": "tower_parts_acquisition.json"}
OFFICIAL_SOURCES = [
    {"id": "tower-operator", "name": "N Seoul Tower construction data",
     "url": "https://www.nseoultower.co.kr/eng/global/intro2.asp",
     "kind": "published", "retrieved_at": "2026-09-08", "reference_time": "undated operator page",
     "facts": {"tower_height_m": 236.7, "foundation_elevation_m": 270.0,
               "published_top_elevation_m": 506.7},
     "limitations": "Published absolute heights have no identified vertical datum; not used as DTM-compatible elevations."},
    {"id": "visitkorea-namsan", "name": "Korea Tourism Organization: Seoul Namsan Park",
     "url": "https://english.visitkorea.or.kr/svc/contents/contentsView.do?menuSn=351&vcontsId=110622",
     "kind": "published", "retrieved_at": "2026-09-08", "reference_time": "undated tourism listing",
     "facts": {"park_opening_hours": "24/7", "entry_fee": "free"},
     "limitations": "General park schedule only; does not confirm individual facilities, trails, temporary closures or wheelchair access."},
    {"id": "visitseoul-namsan", "name": "Seoul Tourism Organization: Namsan Park",
     "url": "https://english.visitseoul.net/nature/Namsan-Park/ENP003631",
     "kind": "published", "retrieved_at": "2026-09-08", "reference_time": "2026-05-05 page edit",
     "facts": {"park_entry_fee": "free", "districts": ["Jangchung", "Yejang", "Hoehyeon", "Hannam"]},
     "limitations": "Park-level facilities do not establish accessibility of every path."},
]


def write_atomic(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    if temporary.exists():
        raise FileExistsError(f"Inspect this tool's interrupted output before retrying: {temporary}")
    try:
        with temporary.open("x", encoding="utf8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def acquire(raw: Path, key: str, remaining_bytes: int) -> tuple[dict, dict]:
    destination, metadata = raw / f"{key}.json", raw / META_NAMES[key]
    if destination.exists():
        if not metadata.exists():
            raise ValueError(f"Cached source lacks provenance: {metadata}")
        return json.loads(destination.read_text()), json.loads(metadata.read_text())
    errors = []
    for endpoint in ENDPOINTS:
        request = urllib.request.Request(endpoint,
            data=urllib.parse.urlencode({"data": QUERIES[key]}).encode(),
            headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=50) as response:
                encoded = response.read(remaining_bytes + 1)
            if len(encoded) > remaining_bytes:
                raise ValueError("Combined context download would exceed the 5 MiB cap")
            value = json.loads(encoded)
            if "elements" not in value or value.get("remark"):
                raise ValueError(f"Incomplete Overpass response: {value.get('remark')}")
            meta = {"endpoint": endpoint, "query": QUERIES[key],
                    "fetched_utc": datetime.now(timezone.utc).isoformat()}
            temporary = destination.with_suffix(".json.part")
            if temporary.exists():
                raise FileExistsError(f"Inspect interrupted source download: {temporary}")
            try:
                with temporary.open("xb") as stream:
                    stream.write(encoded)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            write_atomic(metadata, meta)
            return value, meta
        except (urllib.error.URLError, TimeoutError) as error:
            errors.append(f"{endpoint}: {type(error).__name__} {error}")
            if getattr(error, "code", None) in {406, 429}:
                time.sleep(30)
    raise RuntimeError("OSM context unavailable; cached data are preserved. " + "; ".join(errors))


def polygon(element: dict):
    if element["type"] == "way":
        coords = [(p["lon"], p["lat"]) for p in element.get("geometry", [])]
        if len(coords) >= 4 and coords[0] == coords[-1]:
            result = Polygon(coords)
            return result if result.is_valid else None
    elif element["type"] == "relation":
        rings = {"outer": [], "inner": []}
        for member in element.get("members", []):
            geometry = member.get("geometry", [])
            if member["type"] == "way" and geometry and all("lon" in p for p in geometry):
                role = "inner" if member.get("role") == "inner" else "outer"
                rings[role].append(LineString([(p["lon"], p["lat"]) for p in geometry]))
        outers = list(polygonize(unary_union(rings["outer"])))
        if outers:
            result = unary_union(outers)
            holes = list(polygonize(unary_union(rings["inner"])))
            if holes:
                result = result.difference(unary_union(holes))
            return result if result.is_valid and not result.is_empty else None
    return None


def edge_access(tags: dict) -> tuple[str, list[str]]:
    """Use explicit transport-mode access before generic access; never infer from DTM."""
    if any(k in tags for k in ("foot:conditional", "access:conditional")):
        return "unknown", ["Conditional access requires a complete time-aware rule evaluator"]
    foot = tags.get("foot")
    effective = foot if foot is not None else tags.get("access")
    evidence = []
    if effective in {"no", "private", "customers", "destination", "use_sidepath"}:
        return "prohibited", [f"OSM {'foot' if foot is not None else 'access'}={effective}"]
    if effective in {"yes", "designated", "official"}:
        evidence.append(f"OSM {'foot' if foot is not None else 'access'}={effective}")
        return "verified", evidence
    if effective == "permissive":
        return "estimated", ["OSM permissive access can be revoked"]
    # Highway type identifies pedestrian infrastructure, not a verified right of entry.
    return "unknown", ["No explicit public pedestrian access tag"]


def prepare_context(documents: dict, provenance: dict, raw: Path) -> dict:
    elements = {(e["type"], e["id"]): e for d in documents.values() for e in d["elements"]}
    sources = list(OFFICIAL_SOURCES)
    for key in QUERIES:
        path = raw / f"{key}.json"
        sources.append({"id": key, "name": "OpenStreetMap bounded Namsan extract",
            "url": provenance[key]["endpoint"], "kind": "mapped",
            "retrieved_at": provenance[key]["fetched_utc"],
            "reference_time": documents[key]["osm3s"]["timestamp_osm_base"],
            "license": "ODbL 1.0", "attribution": "© OpenStreetMap contributors",
            "license_url": "https://www.openstreetmap.org/copyright",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size,
            "query": provenance[key]["query"]})
    areas, invalid = [], []
    for (kind, identifier), element in sorted(elements.items()):
        tags = element.get("tags", {})
        if not (tags.get("leisure") == "park" or tags.get("natural") in {"wood", "water", "scrub", "grassland"}
                or tags.get("landuse") in {"forest", "grass"}):
            continue
        shape = polygon(element)
        if shape is None:
            invalid.append(f"{kind}/{identifier}")
            continue
        areas.append({"id": f"{kind}/{identifier}", "name": tags.get("name", tags.get("name:en", "")),
                      "kind": "park" if tags.get("leisure") == "park" else tags.get("natural", tags.get("landuse")),
                      "geometry": mapping(shape), "tags": tags, "source": "osm_namsan",
                      "source_url": f"https://www.openstreetmap.org/{kind}/{identifier}",
                      "visibility_status": "unknown"})
    namsan = polygon(elements[("way", 244397333)])
    if namsan is None:
        raise ValueError("Namsan mapped woodland is unavailable or invalid; inspect the source snapshot")
    nodes, edges = {}, []
    bounds = box(*BBOX)
    geod = Geod(ellps="WGS84")
    for (kind, identifier), element in sorted(elements.items()):
        tags = element.get("tags", {})
        if kind != "way" or "highway" not in tags:
            continue
        node_ids, geom = element.get("nodes", []), element.get("geometry", [])
        if len(node_ids) != len(geom):
            raise ValueError(f"Way {identifier} has incomplete node geometry")
        for i, (u, v) in enumerate(zip(node_ids, node_ids[1:])):
            a, b = geom[i], geom[i + 1]
            line = LineString([(a["lon"], a["lat"]), (b["lon"], b["lat"])])
            if u == v or not bounds.covers(line):
                continue
            for node_id, position in ((u, a), (v, b)):
                tags_node = elements.get(("node", node_id), {}).get("tags", {})
                nodes[str(node_id)] = {"id": str(node_id), "lon": position["lon"], "lat": position["lat"],
                                       "tags": tags_node}
            access_status, access_evidence = edge_access(tags)
            inside_park = bool(namsan.covers(line))
            hours = tags.get("opening_hours")
            hours_status = "verified" if hours == "24/7" else "unknown"
            hours_evidence = ["OSM opening_hours tag"] if hours else []
            if inside_park and tags["highway"] in {"footway", "path", "steps", "pedestrian"}:
                if access_status == "unknown":
                    access_status = "estimated"
                    access_evidence = ["Mapped pedestrian path inside named Namsan woodland",
                                       "Official park listing confirms public park, but not this particular path"]
                if not hours:
                    hours, hours_status = "24/7", "estimated"
                    hours_evidence = ["visitkorea-namsan park-level hours; individual path schedule unverified"]
            barriers = [dict(node_id=str(node), **nodes[str(node)]["tags"]) for node in (u, v)
                        if "barrier" in nodes[str(node)]["tags"]]
            _, _, length = geod.inv(a["lon"], a["lat"], b["lon"], b["lat"])
            foot_direction = tags.get("oneway:foot", "no")
            directed = foot_direction in {"yes", "1", "true", "-1"}
            if foot_direction == "-1":
                u, v = v, u
                a, b = b, a
            edges.append({"id": f"{identifier}:{i}", "u": str(u), "v": str(v), "way_id": identifier,
                "length_m": length, "geometry": [[a["lon"], a["lat"]], [b["lon"], b["lat"]]],
                "directed": directed, "tags": tags, "access_status": access_status,
                "access_evidence": access_evidence, "opening_hours": hours,
                "opening_hours_status": hours_status, "opening_hours_evidence": hours_evidence,
                "inside_namsan_woodland": inside_park, "steps": tags["highway"] == "steps",
                "barriers": barriers, "source": "osm_namsan",
                "source_url": f"https://www.openstreetmap.org/way/{identifier}"})
    shaft = polygon(elements[("way", 370286010)])
    if shaft is None:
        raise ValueError("Mapped tower shaft polygon is invalid or absent")
    target = shaft.centroid
    start = elements[("node", 3403067530)]
    landmarks = [{"id": "n-seoul-tower", "name": "N서울타워 상단 대표점", "type": "tower_point",
        "lon": target.x, "lat": target.y, "height_m": 236.7, "height_reference": "agl",
        "height_status": "estimated", "supported": True, "features": ["city", "landmark"],
        "geometry": mapping(shaft), "sources": ["tower-operator", "osm_tower_parts"],
        "source_url": "https://www.openstreetmap.org/way/370286010",
        "coordinate_method": "Centroid of mapped 2D tower shaft, then engine raster-cell quantization",
        "height_method": "DTM at effective source cell + operator-published 236.7 m structural height",
        "uncertainties": ["Foundation elevation may differ from bare-earth DTM; modeled apex is approximate",
                          "Published absolute elevation and OSM ele have unresolved references; neither is used",
                          "Single upper point visibility does not establish tower body or skyline visibility"]},
        {"id": "namsan-woodland", "name": "남산 숲과 산 능선", "type": "broad_scene",
         "geometry": mapping(namsan), "features": ["nature", "forest", "mountain"], "supported": False,
         "sources": ["osm_namsan", "visitseoul-namsan"], "visibility_status": "unknown",
         "reason": "Mapped woodland context only; foliage and distributed scene targets are not modeled"}]
    return {"schema_version": 1, "mode": "real", "region": "Namsan–Myeongdong, Seoul",
        "crs": "EPSG:4326", "coordinate_order": "longitude, latitude", "bounds": BBOX,
        "prepared_at": datetime.now(timezone.utc).isoformat(), "sources": sources,
        "landmarks": landmarks, "graph": {"nodes": list(nodes.values()), "edges": edges}, "areas": areas,
        "default_start": {"name": "명동역 3번 출구 지상", "lon": start["lon"], "lat": start["lat"],
                          "source_url": "https://www.openstreetmap.org/node/3403067530",
                          "wheelchair_status": "unknown"},
        "weather": {"status": "unknown", "reference_time": None},
        "crowding": {"status": "unknown", "reference_time": None},
        "quality": {"invalid_or_unclosed_area_ids": invalid,
                    "access_policy": "Explicit foot/access tags; park membership alone is estimated",
                    "opening_hours_policy": "24/7 explicit way tag verified; general park hours estimated per path",
                    "route_policy": "Original OSM node adjacency, geodesic segment lengths, pedestrian one-way only",
                    "limitations": ["No live closure, crowd, weather or public-access observations",
                                    "Network extent is bounded; routes leaving the extract are unavailable",
                                    "Barriers and access tags may be missing or outdated",
                                    "Mapped nearby greenery or water is not evidence of visible scenery",
                                    "Trees, walls, construction and overhangs are absent from the visibility surface"]}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/demo")
    args = parser.parse_args()
    budget = preflight(ROOT / "data", additional_bytes=32 * 1024**2, temporary_bytes=LIMIT)
    raw = args.output / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    documents, provenance = {}, {}
    for key in QUERIES:
        used = sum(p.stat().st_size for p in raw.glob("osm_*.json"))
        if used > LIMIT:
            raise ValueError("Cached OSM responses exceed the configured combined 5 MiB cap")
        documents[key], provenance[key] = acquire(raw, key, LIMIT - used)
    context = prepare_context(documents, provenance, raw)
    context["storage_preflight"] = budget
    preflight(ROOT / "data", additional_bytes=24 * 1024**2, temporary_bytes=LIMIT)
    write_atomic(args.output / "context.json", context)
    print(json.dumps({"context": str(args.output / "context.json"), "mode": context["mode"],
        "raw_bytes": sum(p.stat().st_size for p in raw.glob("osm_*.json")),
        "nodes": len(context["graph"]["nodes"]), "edges": len(context["graph"]["edges"]),
        "areas": len(context["areas"]), "landmarks": len(context["landmarks"]),
        "source_timestamps": [s["reference_time"] for s in context["sources"]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
