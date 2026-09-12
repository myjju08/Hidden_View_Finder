# Prototype execution record — 2026-09-11

This is a locally executed real-data prototype. The final fixture, browser,
resource and district results are recorded in the JSON reports beside this file.
**680 tests passed, four warnings, zero failures or skips** in the final run
(25.26 s pytest; 31.90 s including the bounded monitor). This includes the 518
existing tests freshly rerun and 162 prototype tests. See
[readiness.json](readiness.json) for the preserved full-run summary and the retained `data/prototype/test-artifacts/pytest-20260911T154655.log`. `tests-final.json` is overwritten by later focused checks. Two warnings concern FastAPI/Starlette
deprecations; two concern existing fork-based safety fixtures in a multithreaded
test process. No warning is represented as field or visibility validation.
No acquisition command, raster rebuild, public deployment, commit or push ran.

## Inputs and supported scope

The existing package `seoul-20260911-v1-6abf3cc88682` and prepared collection
`67cd2654e14de3c0` were reused in place. The existing offline package validator
verified all 15 artifacts; the tile adapter verified 654 raster files across
109 tiles, their hashes, EPSG:5186 metre grid, alignment, elevation reference,
quality bits and lineage. See [input-inspection.json](input-inspection.json).
There are 435,438 building records, 582,255 candidate records, 25 districts,
239,880 paths, 3,055 water features, 12,853 green-space features, 639 peak/ridge
features and 2,248 source landmarks. Candidate records are not all unique or
eligible: runtime deduplication preserves lineage, and restrictions and effective
raster endpoints are checked before recommendation.

Global `geographic_ready`, `normalized_ready` and `visibility_ready` remain
false; existing `acquisition_ready` remains true under its original definition.
The application exposes `local_exploration_available` separately. Every ray's
required columns, including tile seams and interpolation dependencies, must be
supported. Sparse components never certify a complete panorama. Terrain support
is 95.91% of Seoul and 27.51% of the requested buffer; neither is a visibility
success rate or probability.

All seven invalid-building source flags and their conservative affected regions
remain quarantined. The read-only PBF investigation recovered selected members
for localizing problematic OSM relations: 26 ways and 2,593 nodes, with three
reconstructable geometries. All 46 diagnostic IDs remain excluded; the original
36-incomplete-relation readiness flag is unchanged. No repaired vectors were
mixed with stale roof rasters. See [osm-recovery.json](osm-recovery.json) and
[geometry-inspection.json](geometry-inspection.json).

## Implemented and exercised

The Korean mobile/desktop application has a bundled Leaflet map, bounded local
roads/water/green-space/district layers, local name search, origin selection,
straight-line radius, Asia/Seoul intended viewing time, scene/composition
preferences, ranked diverse view cards, orientation cones, evidence details,
coverage/provider/data status and geometry schematics. It sends no citywide
candidate dump or remote basemap requests. Different sessions retain their own
origin-dependent proximity, score and description for a stable view identity.

The indexed pipeline samples candidates spatially, applies access/endpoint
exclusions, evaluates multiple column-top roof/terrain samples and retains all
visible/blocked/excluded/unknown samples in one intended denominator. Target
inventory extends to standing radius plus 10 km; each ray still has its own
10 km limit. Sun position and supported directional horizon are supplementary
context. Scores are fixed-weight heuristics, not satisfaction probabilities.

OpenAI text/image adapters, structured-output validation, hard HTTP deadlines,
durable spending reservations, kill switches, image version keys, coalescing,
LRU eviction and interruption recovery are implemented and mock-tested. The
actual run had no application API key, enablement or authorized spending:
**zero live text/image calls**. Live provider interoperability remains untested.
No-key templates and geometry schematics were exercised with real data.
Weather, atmospheric visibility, crowds, illumination and current opening remain
unknown. River-only recommendations are intentionally empty without compatible
water-surface elevation; mapped water is still displayed on the map.

## Verification commands actually used

```bash
bash scripts/data/python.sh scripts/prototype/bootstrap.py
bash scripts/prototype/python.sh scripts/prototype/run.py validate --report
bash scripts/prototype/python.sh scripts/prototype/benchmark.py
bash scripts/prototype/python.sh scripts/prototype/browser_run.py --engine firefox --base http://127.0.0.1:8000 --origin 126.81774514857614,37.56567741795218
bash scripts/prototype/python.sh scripts/prototype/check.py
```

During development, bounded helpers used `--development` only while the live
shared development reservation owned the writer lock. Routine reproduction uses
the commands above with their own shared reservations. Fixtures use small
synthetic GIS/HTTP/provider data; they do not acquire Seoul data or spend money.
Existing tests were freshly rerun, not inferred from the historical 518 passes.

Early iterations exposed and fixed strict numeric validation, cancelled-worker
ownership, per-session result isolation, target-pool extent and viewport map
sampling, together with combined artifact and lowered image-cache caps. These
were real development failures/findings, not skipped final tests.

Chromium first lacked native libraries and then could not use its sandbox in
this container. No sandbox was disabled. A single official Firefox fallback ran
with default security settings. Its kernel namespace warning is documented;
this task does not assert OS sandbox properties that were not verified. Browser
libraries were extracted from pinned official packages into accounted paths;
no system package installation or maintainer scripts ran.

The final Firefox suite passed in 23.644 s at desktop width 1,440 px and mobile
width 390 px, without horizontal overflow. Real map/origin/search/recommendation,
evidence, coverage/data status and disabled-image fallback checks passed. Small
synthetic checks covered rate limits, empty results, source-text injection, mixed
target support and all four evidence states. No page JavaScript errors or
external page requests occurred. Root and frontend reviewers inspected the new
map and card screenshots. The six screenshots total 462,642 bytes; browser
process-tree RSS peaked at a sampled 734,691,328 bytes (shared pages may be
counted twice). See [browser-validation.json](browser-validation.json).

The combined artifact guard initially refused Firefox's inert profile lock
symlink; the narrow lstat-only accounting fix retains output-path protection.
A new synthetic escape fixture then caused the shared development monitor to
refuse and release its lock. Two manifest-identified test symlinks were removed
without reading/deleting their targets, source data or prior outputs; fixture
paths now remain inside overall checkout accounting while escaping the isolated
fixture budget. The final browser recovered the stale lock and used a normal
reservation. The first full-test wrapper attempt correctly refused while an
atomic browser-report writer held that same lock; its next invocation completed
all 680 tests. See [safety-recovery.json](safety-recovery.json). These refusals
happened before a new substantial write; none was bypassed.

The initial browser extraction plan refused its 400 MB sub-budget before a
large write. Measured archives/expansion led to a 500 MB Chromium allocation and
then a 900 MB aggregate allocation for the one Firefox fallback. All remained
inside the same mandatory 20,000,000,000-byte total and 8 GiB free-space floor.

An initial instruction-file discovery listed sibling workspace filenames more
broadly than intended. No sibling file contents were read or used; subsequent
inspection stayed within this checkout and applicable parent instructions.

## Final real-data outcomes

[citywide-queries.json](citywide-queries.json) records one indexed, source-derived
origin in each of 25 districts: **23 partial supported responses, two empty
responses (Geumcheon and Yeongdeungpo), zero HTTP errors**. These are sampled
queries, not district coverage percentages. [demonstrations.json](demonstrations.json)
contains repeated successful Gangseo city/skyline/mountain scenes and Dongjak
mountain scenes, plus the empty Yeongdeungpo request. All use the requested 3 km
standing radius. The independently unsupported terrain origin still finds
supported standing locations without moving the origin; see
[unsupported-origin.json](unsupported-origin.json). [river-only.json](river-only.json)
records the honest missing-water-elevation empty case.

Median indexed candidate retrieval was **0.5915 s** (maximum 0.9112 s), target
inventory 1.4695 s, geometry 4.4797 s, and HTTP end-to-end **6.6453 s**
(range 6.2226–7.0157 s). The sub-second indexed-retrieval goal passed for this
sample; approximately five-second end-to-end did not. Presentation uses local
templates; the small HTTP-minus-service remainder includes serialization and
transport rather than a separately isolated presentation benchmark. Initial
adapter and warm geometry microbenchmarks are separately recorded in
`geometry-inspection.json`; OS caches were not flushed, so initial HTTP queries
are not claimed to be cold-filesystem measurements.

The owned server's maximum sampled RSS was **244,756,480 bytes** across 231
one-second samples and repeat boundaries. This is a sampled process measure,
not a guaranteed instantaneous peak or the whole shared-container memory use.

## Retained storage

[storage-final.json](storage-final.json) records approximately **3.627 GB** total
accounted footprint, including a retained conservative 5 MiB external allowance,
against the mandatory **20,000,000,000-byte** ceiling. Prototype additions are
about **1.041 GB**, mostly optional browser tooling. The source package and
prepared raster files remain in place at **1.343 GB accounted**. Remaining disk
space is about **23.34 GB**, above the 8 GiB reserve. No new map derivatives,
persistent scene cache or generated images were produced.

Final browser combined artifacts peaked at a sampled **41,682,423 bytes** under
the 100 MB cap. The final fixture run's sampled project/staging peaks were
3,627,313,543 / 556,904,617 bytes. The initial development reservation bounded
its projected total peak to 3,852,750,224 bytes, but its final in-memory peak
counters were lost on the safe fixture refusal. That bound is **not** an observed
peak; continuous telemetry and an OS quota are not claimed. The later browser
and complete fixture run each used independent locked reservations. Earlier
archives and test outputs were retained; only the two recorded synthetic links
and automatically disposable owned browser profiles were removed.

The retained network receipts total at least 257,109,667 bytes for small Python
packages, browser tooling/native libraries and bundled map/font assets. Small
metadata/probe/retry traffic was not fully metered, so this is a lower bound,
not an exact network total. No geographic source was redownloaded. A report can
be refreshed after the recorded validations with:

```bash
bash scripts/prototype/python.sh scripts/prototype/report.py
```

## Operational limits and reproduction

See [../../docs/prototype.md](../../docs/prototype.md) for setup, start, API,
AI enablement, local support, licence gates and exact browser commands.
The server binds only `127.0.0.1:8000`, has one owning geometry worker, bounded
map readers, rate/body/work limits, no persistent query cache and no access log.
All datasets stay outside the static root. No exact user origin is persisted
or sent to providers; recorded diagnostics use selected public-source origins.

Restart local operation with:

```bash
bash scripts/prototype/python.sh scripts/prototype/run.py serve --port 8000
```

Resume the version-aware district exercise with the same `benchmark.py`
command. Revalidate read-only inputs with `run.py validate --report`. No command
here resumes acquisition automatically. A terminated Codex session does not
promise to restart the server or benchmark autonomously.

Public deployment is still blocked pending recorded review of actual terrain,
GBA conversion/derivative and ODbL publication obligations. A noncommercial or
experimental label does not clear that gate. Compatible surrounding terrain and
water elevations remain missing; tree canopy, full facades and field accuracy
are not modeled. No field validation or global readiness is claimed.
