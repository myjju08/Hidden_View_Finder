Source inventory inspected on **2026-09-11 UTC** for configuration `02a65fe1a545`.
This report summarizes completed acquisition/normalization and candidate receipts.
All 109 terrain/obstruction tiles are prepared, including 33 validated reused tiles
and 76 newly built tiles. Terrain is valid at **95.9068% of Seoul cell centres**
and **27.5127% of requested support cell centres**; complete support did not pass.
Consult [acquisition.json](acquisition.json) for readiness, storage accounting and
the exact resume command.
All byte counts below are logical artifact sizes, not a peak-storage estimate.

| Input and status | Retained input / normalized artifact | Source date and distribution |
| --- | --- | --- |
| Seoul boundary: acquired, validated and reused | Raw Nominatim JSON **92,003 B**; three requested extent geometries in `data/citywide/geometry/extents.02a65fe1a545.geojson` | Separately inspected 2026-09-11 response for [OSM relation 2297418](https://www.openstreetmap.org/relation/2297418); retrieval date is not a survey date. The previous response fingerprint remains pinned separately. |
| Terrain: acquired and normalized; surrounding support blocked | ZIP **45,852,601 B**; complete SHP sidecars/encoding metadata **78,559,101 B** expanded; `terrain.gpkg` **82,120,704 B** | [Seoul OA-22241](https://data.seoul.go.kr/dataList/OA-22241/F/1/datasetView.do), NGII **2023** topographic inputs; file update 2025-03-20, catalogue metadata update 2026-02-11. Downloaded 2026-09-11. KOGL Type 1, attribution required. |
| Buildings: selected spatial ranges acquired and normalized; geometry readiness blocked | Indexed `buildings.gpkg` **128,983,040 B**; all **46 selected row groups** committed out of 291 regional groups | [Source Cooperative conversion](https://source.coop/tge-labs/globalbuildingatlas-lod1), regional object modified 2025-09-06; accessed 2026-09-11. Mixed footprint dates, predominantly **2019** height imagery with 2018 fallback. Public deployment requires source-specific licence review. |
| OSM: country snapshot acquired, useful normalized features published; geometry readiness blocked | South Korea PBF **286,988,090 B**; extraction polygon **5,657 B**; indexed `osm.gpkg` **209,899,520 B** | [Geofabrik South Korea](https://download.geofabrik.de/asia/south-korea.html), dated `south-korea-260910.osm.pbf`, database timestamp **2026-09-10T20:21:06Z**; downloaded 2026-09-11. ODbL 1.0, © OpenStreetMap contributors. |
| Additional official buffer terrain: blocked | No compatible surrounding elevation product acquired | The NGII portal returned HTTP 400; no permitted, bounded, unauthenticated surrounding-tile endpoint was verified. Preserve the gap. |
| Optional context: deferred | No weather/crowd history, imagery, model weights or redundant geographic products acquired | Weather and crowd uncertainty do not block geographic preparation; absent crowd data is not evidence of quietness. |

The normalized directory is `data/citywide/normalized/02a65fe1a545/`.
Its `terrain.source.json`, `buildings.source.json` and `osm.source.json` contain
schemas, source identities, input/output hashes, parameters and quality counts.
Raw download receipts are beside their artifacts under `data/citywide/raw/`.
[Source review](../../docs/citywide-source-review.md) records catalogue/licence
links, bounded source probes and the documented runtime mirror fallback.

Local SHA-256 fingerprints (complete local artifacts):

| Artifact | SHA-256 |
| --- | --- |
| Boundary raw JSON | `4969ce7ad7fa1ec2b9e2c81e715226364a65395aa4fcc442abe9efa578e39bf2` |
| Terrain ZIP | `4fbe3c7e061b5974e7403ec116855304ed8ae321eebcc0d12c31ca8fb7be30bf` |
| Normalized terrain | `28bdbed027a52cc9e811aa3b144735827835237e8ff7c3696e8150547a7cc372` |
| Normalized buildings | `fd080e4b09e8eeeffb68f5b3a77dd9a8b1404136a0dbe38d2436d787f4774ba8` |
| South Korea PBF | `db198a06408e536f0ddb36bdd5daf95c970a5873f906c7b68e40b4f4759c24a6` |
| Normalized OSM | `ea7c11eac09ad5c7c60a2e6caaa0d185ee0c1ef6b14a9ca2fc7bac6617eaba3e` |

The PBF additionally passed the independently published MD5
`6b4cb4d74d7355b0da24b9df3d48cbb6`. The terrain ZIP fingerprint is a
repository-inspected source pin, not a publisher checksum. Buildings used strict
HTTP 206 ranges pinned to strong ETag `"4f26ca9c75356a918262caf894f8d678-44"`
and S3 version `0i3hEPRd_oSdm7HyyIBFIWp.gt.Hg19l`. Individual transferred ranges
and stored row groups have fingerprints; **no whole remote building-object
checksum was verified and no complete regional raw file was downloaded**.
Its receipt records 62,368,044 planned selected-range bytes; current-invocation
transfer counters exclude earlier interrupted requests, so use the aggregate
storage report and its caveats for network totals.

All interface geometries use WGS84 longitude/latitude. Terrain SHP is inspected
EPSG:5174 with CP949 text, transformed horizontally to EPSG:5186. The two layers
contain **8,570 MultiLineString contours** and **45,870 spot points**, with no
missing elevations, invalid geometries, repairs or duplicate source IDs. Contour
values span 5–770 m; spot values span −0.54–740.2 m. Elevations follow the NGII
Incheon mean-sea-level convention, with no embedded vertical CRS and no vertical
conversion. These elevations are distinct from estimated building height above
ground, derived absolute roof elevation, and observer eye height. Five-metre
raster spacing will not establish five-metre elevation accuracy.

The converted building distribution uses inspected OGC:CRS84 coordinates,
transformed to EPSG:5186; original-product EPSG:3857 advice was not blindly
applied. **435,438 footprints** retain source classes (`osm` 260,501; `ours2`
160,923; `3dglobfp` 14,014), metre AGL heights and supplied uncertainty. All
heights are model estimates: no unresolved heights in this subset does not
certify their accuracy. **Seven invalid geometries** remain explicitly retained,
with zero acquisition repairs. These unresolved source geometries independently
gate `normalized_ready=false`; surface-stage repairs, if recorded, do not
retroactively repair the normalized source. **50,327 source/ID groups are nonunique**;
unique local GeoPackage FIDs are kept for processing. **71 supplied variances
are negative**, **5,255 heights are below 1 m**, and observed heights span
0.0081948–173.1149 m. Values remain preserved and flagged; negative variances
are not used as valid uncertainty in surface calculations. Missing/nonpositive
heights would remain unresolved, never be imputed to zero.

For successfully assembled OSM geometries, relation-aware processing preserved
required way nodes, multipolygon members, holes, source references and available
access/foot/opening-hours,
steps, incline, wheelchair, barrier, bridge, tunnel and layer evidence. Missing
tags stay missing. It emitted **289,022 thematic layer features**, plus **46
quality diagnostics**: paths 239,880; public spaces 7,541; water 3,055; green
space 12,853; peaks/ridges 639; bridges 11,338; landmarks 2,248; barriers 7,498;
boundaries 3,970. Cross-layer emissions can refer to the same source object;
these counts are not unique mapped-object totals. Nineteen duplicate category
matches were merged; the published layers report zero invalid geometries and
zero duplicate source IDs within each layer.

There are nevertheless **36 unassembled relevant relations** and **34 geometry
issue records**, consolidated into the 46 explicit quality features; zero invalid
published geometries does not erase those failures. No repairs were invented.
The [relation inspection](osm-relation-inspection.json) documents 14 relations
with missing way members. Their names/census tags suggest country-extract edge
objects, but their unknown complete geometries have not been proven outside
the requested support. The remaining construction failures are also explicit.
OSM `ele`/`height` tags were not converted into terrain or building elevations.
Mapped paths, parks and landmarks do not establish public access, current
opening, scenic quality, visibility or field verification.

The full standing-location contract is **606.325922 km²** of Seoul; modeled
obstruction support adds 10 km (**2,113.601739 km²**), and terrain interpolation
adds another 1 km (**2,286.508518 km²**). The separately retrieved Geofabrik
`.poly` contains all three requested geometries. That proves the current
published extraction-domain relationship, not observation completeness,
relation completeness or a version match between `.poly` and the dated PBF.
Selected building row-group envelopes intersect **99.960042744%** of requested
support, leaving **844,537.256 m²** outside those envelopes; this is neither an
observed coverage mask nor evidence of empty land. The declared terrain acquisition
contract has **1,680,182,596.368 m²** outside the official Seoul source domain.
Even inside Seoul, administrative coverage alone is not a valid TIN support mask.
Spatially distributed coverage layers retain these distinctions and unknowns.

The [full read-only terrain conflict preflight](terrain-conflict-preflight.json)
reused the exact 10 m sampler and source order: **2,076,954 samples**,
**2,069,439 unique six-decimal coordinates**, **7,515 duplicates**, and
**zero elevation conflicts** at the unchanged 0.01 m first-value tolerance.
Observed peak RSS was 217,100,288 B; that preflight created no sample index.
The subsequent preparation built a verified 234,852,352-byte SQLite sample index
with those 2,069,439 unique coordinates. Its 20 held-out spot predictions gave
a diagnostic RMSE of 2.3752 m, not a population or field-accuracy estimate.

Candidate generation published **582,255 points**: 579,891 path chainages and
2,364 explicit pedestrian-area samples, with zero invalid geometries or duplicate
IDs reported. The indexed EPSG:5186 file is **443,691,008 B**, SHA-256
`8f23a5a6e6cb60071b894bf8452d8016d460ce671bb8a23f085d3f4a14b2b5c6`.
`candidates.source.json` records source hashes, access evidence and 20 m spacing.
These are mapped candidate standing locations, not verified accessible or scenic
viewpoints; unsupported elevated structures remain flagged. All 25 district
boundaries are represented in the coverage report. Three candidate points remain
unassigned to those district geometries, and 1,330 landmark-source objects have
representative points inside Seoul. These groupings do not establish exhaustive
standing-location or landmark coverage.

The latest inspected normalized package is
`data/citywide/packages/seoul-20260911-v1-6abf3cc88682/`,
**866,156,702 logical bytes** including manifests, quality layers and licences.
It is a compact input package with explicit blockers; publication does not mean
that `normalized_ready`, `visibility_ready` or deployment approval passed.
Older package versions remain preserved.

Preparation keeps the existing sampled-contour local linear TIN and obstruction
builder at 5 m cell spacing. The unchanged 100,000-point cap requires one tile,
`x39_y111`, to use 128-pixel windows; the other 108 use 256. This changes the
local point subsets and may change interpolation values, so the effective window
is recorded and no numerical identity with an unbuilt larger-window result is
claimed. The 1 km footprint workspace and additional 1 km local-query halo have
a conservative full-grid query union of 3,221 km². With the present Seoul-only
source-domain shortcut, only 47 workspaces run TIN: their query union is
1,511 km², inside the requested support but containing 904.6741 km² outside
the official source domain. Exact processing union geometries are in the
preparation plan, separately from the declared 11 km acquisition geometry;
query extent is not a supported-elevation mask. New source coverage requires
replanning those processing extents, not assuming the declared halo covers
every padded workspace.

Required external resolutions are specific:

1. Supply a permitted official NGII contour/spot or bare-earth elevation download
   for the unsupported geometry in `coverage.gpkg` layer `terrain_support`, or a
   documented accessible official endpoint. Include SHP sidecars where relevant,
   source date, licence, horizontal CRS, units and compatible vertical-reference
   evidence. The declared 10 km support and 1 km interpolation halo stay fixed;
   a DSM or different vertical datum cannot silently fill this gap.
2. Resolve the exact OSM source IDs in `osm.source.json` (and missing members in
   `osm-relation-inspection.json`) using permitted complete same-version source
   geometry or separately inspected corrected upstream versions. Reconstruct and
   validate their spatial relationship before declaring them outside the contract;
   names alone are insufficient. Retain the existing diagnostics until that passes.
3. For full building-support assurance, obtain documented licensed geometry/height
   evidence for the recorded envelope gap and investigate the seven invalid source
   geometries; a completed HTTP-range recipe does not certify absent buildings.
4. Before public redistribution/deployment, review the older mixed Source Cooperative
   conversion against the [current split GBA releases](https://github.com/zhu-xlab/GlobalBuildingAtlas),
   including ODbL footprint obligations, CC BY-NC 4.0 height/other-footprint conditions
   and attribution. Use permitted downloadable releases if a replacement is needed;
   do not automate the prohibited viewer/WFS extraction.

Core accessible acquisition is complete: `acquisition_ready=true` and
`normalized_artifacts_complete=true`. However, `normalized_ready=false`
because seven invalid building geometries and unresolved OSM relations/geometry
issues remain explicit. `visibility_ready=false` because complete terrain and
obstruction support has not passed; successful raster tiles do not override that
coverage gate. Deployment licence review is required and `field_verified=false`.
The final run report records raster completion and any later package version.
The shared exact 20,000,000,000-byte ceiling, 8 GiB free-space floor and 4 GiB
aggregate temporary limit are application guards; no OS-enforced quota is claimed.

The completed rasters are in `data/citywide/prepared/67cd2654e14de3c0/`.
The directory contains **468,384,453 logical bytes**, including its final manifest;
its manifest's earlier `retained_bytes` value excludes the 214,130-byte manifest
itself. Of 24,253,086 Seoul cell centres, 23,260,369 have supported terrain;
992,717 remain NoData (24.817925 km² of 5 m cells). Of 84,544,177 requested
support cell centres, 61,283,808 remain NoData (1,532.0952 km² of cells).
These cell-centre measurements differ from exact vector polygon areas.
The [overlap-quality audit](raster-overlap-quality.md) records observed
internal differences and NoData disagreements without seam or accuracy certification.
