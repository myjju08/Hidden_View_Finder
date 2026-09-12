# Hidden View Finder local working prototype

This mode discovers **view opportunities** from the acquired Seoul package: a mapped standing point, direction, intended viewing time, and supported scene samples. It uses the existing Korean application's visual language and solar calculation, the existing GeoPackage indexes, and the visibility engine's closed-column traversal contract. The old fictional scenario demo is preserved separately; none of its artwork or results is used here.

The core requires no AI key. Public deployment remains blocked by review of the actual published OSM/GBA layers and derivatives. This task does not authorize publication, proxy/firewall changes, commits, or pushes.

## Repository contents and local data prerequisites

Git includes code, tests, licensed renderer/font assets and reviewed aggregate reports.
Acquired GIS, detailed scene extracts and feature-level quality records remain local
because their publication licence review is unresolved. The working checkout
retains every such file; ignoring a file for Git does not remove it.

A fresh clone needs the validated source package and prepared tile collection at
the paths in `configs/prototype.json`, together with these matching local QA records:

- `reports/prototype/osm-recovery.json`
- `reports/citywide/raster-overlap-anomalies.json`

Restore those records together with the validated data from your own existing
workspace. They are part of the local input bundle, even though their paths are
under `reports/`. Startup verifies their source hashes and fails safely when they
are missing; they must not be replaced with empty records. The inspection programs
`hidden_view_finder.prototype.osm_recovery` and `reports/citywide/audit_raster_edges.py`
provide regeneration ingredients, but a single guarded publisher for both exact
consumed QA schemas is not implemented yet. The aggregate `report.py` refresher
also expects local detailed demonstration reports. These limitations do not affect
the current validated checkout. See `reports/citywide/publication-summary.json`
for publishable counts, checksums and unchanged readiness.

## Run locally

From this checkout, reuse the acquired GIS environment. None of these commands runs acquisition or rebuilds citywide rasters.

```bash
bash scripts/data/python.sh scripts/prototype/bootstrap.py
bash scripts/prototype/python.sh scripts/prototype/run.py inspect --report
bash scripts/prototype/python.sh scripts/prototype/run.py validate --report
bash scripts/prototype/python.sh scripts/prototype/run.py serve --port 8000
```

Open `http://127.0.0.1:8000`. The API deliberately rejects other hosts/origins. Use an authorized local tunnel if your browser runs elsewhere; no public tunnel is created by this task. The server reads the existing city package and prepared tiles **in place**, outside the static root.

The Python bootstrap installs 13 small, pinned, publisher-SHA256-verified wheels into `data/prototype/dependencies/site`; it does not install another NumPy/GDAL stack. `configs/prototype-requirements.json` records exact versions, objects, sizes, and checksums. Setup compares existing files instead of overwriting incompatible content. There is no mandatory GIS derivative/index build: existing SQLite/RTree indexes suffice. Bundled Leaflet and Noto Sans KR assets have separate reproducible, bounded fetchers:

```bash
bash scripts/prototype/python.sh scripts/prototype/fetch_leaflet.py
bash scripts/prototype/python.sh scripts/prototype/fetch_font.py
```

Core map rendering uses these bundled assets and bounded local vectors, with no remote basemap tiles or API keys. The display is a simplified geographic map, not a complete navigation map.

## Workflow and API

Choose an origin on the map, search local source names, or grant browser geolocation. Set a **straight-line** radius, viewing date/time in **Asia/Seoul**, scenery preferences, composition, and optional crowd preference. `view_at` means when the user intends to see the view. There is no departure-time, ETA, walking-distance, or route prerequisite.

```bash
curl -sS http://127.0.0.1:8000/api/capabilities
curl -sS 'http://127.0.0.1:8000/api/places?q=남산'
curl -sS -H 'Content-Type: application/json' \
  -d '{"origin":{"lon":126.99,"lat":37.55},"radius_m":3000,"view_at":"2026-09-11T18:00:00+09:00","preferences":["mountain","city","greenery"],"composition":"any","crowd_preference":"any","limit":3}' \
  http://127.0.0.1:8000/api/recommendations
```

These example coordinates are a real geographic query, not a canned result. Results depend on the current evidence version and bounded workload; three cards are never guaranteed.

View detail and image submission require the same anonymous browser session that requested the recommendation. The browser keeps the cookie automatically. For a sequence of `curl` calls, add `-c data/prototype/curl.cookies -b data/prototype/curl.cookies` to the recommendation and subsequent detail/image requests. This small cookie jar stays inside the accounted, ignored runtime directory; do not publish it. A request without the originating session receives `404` for that view even when its stable geographic ID is known.

| Endpoint | Contract |
| --- | --- |
| `GET /api/health` | Service/worker availability |
| `GET /api/capabilities` | Unchanged source readiness, local-query capability, data dates, provider and storage status, publication blocker |
| `GET /api/map?bbox=w,s,e,n&zoom=11` | Bounded, simplified local roads, districts, water and green-space GeoJSON; at most 700 features and approximately 1 MB |
| `GET /api/coverage` | 109 tile aggregates; **not** a cell-validity or view-confidence map |
| `GET /api/places?q=...` | At most 20 local source-name matches; bounded read-only SQLite scan, not an external geocoder |
| `POST /api/recommendations` | Validated distance-only search; up to three diverse supported cards and 20 additional map opportunities |
| `GET /api/views/{view_id}` | Same typed `SceneEvidence`; expires from bounded memory after later queries |
| `POST /api/images` | `{ "view_id": "..." }`; on-demand authorized image or explicit unavailable state |
| `GET /api/images/{key}` | Bounded job state; `.jpg` and `.thumb.jpg` suffixes serve owned cached derivatives |

Errors have structured codes. Cross-origin requests, oversized/forged bodies, arbitrary paths/URLs, invalid radii, and extra route fields are rejected. The host/proxy assumption is direct localhost: forwarded client headers are ignored. Per-process IP rate limits are 10 costly POSTs/minute and 180 reads/minute, capped at 1,024 in-memory counters. One owning geometry worker and two concurrent map/search readers bound work. A cancelled HTTP request cannot release the geometry slot while its actual worker is still running. Initial connections and incomplete HTTP headers have a five-second absolute deadline, so idle browser connections cannot hold all 16 slots indefinitely. Parsed requests keep their separate body and geometry deadlines.

## What local support means

Source `normalized_ready`, geographic completeness, and `visibility_ready` remain false. `local_exploration_available` is separate: the adapter validates the collection recipe, CRS, vertical reference, 5 m grid alignment, file hashes, quality bits, occupancy, overlaps, and unknown regions before enabling individual queries. A valid ray does not establish a valid panorama.

`Tiles` reads bounded blocks and retains at most 12 GDAL handles and 32 MiB of block data. Handles are opened, used and closed in their owning geometry worker. The implementation reuses the engine's supercover traversal and exact per-cell quadratic curvature calculation, including edge/corner contact. Every required column must be supported; an unknown column takes precedence even when another column already blocks the ray. It never feeds NoData into a dense viewshed or ignores engine validation.

Observer XY remains the requested path coordinate. Height is ground from its containing valid, unoccupied raster column plus 1.7 m eye height. The effective XY is identical, with zero snapping; the different cell-center coordinate/displacement is diagnostic. Standing points in mapped buildings/water, uncertain structures, road-carriageway vicinity, or insufficient exclusion-query coverage are rejected. There is no move to another path to obtain a result.

Roof and terrain targets use multiple **model column top-edge samples**, at the unchanged surface elevation. The near-facing edge is analytically intersected; obstacles and the target building remain in traversal, so self-occlusion still occurs. Building source footprint and raster-cell association are checked. This is neither whole-building/facade visibility nor a precise visible-area percentage. Model building roofs are the prepared maximum-footprint-ground plus estimated AGL height, not surveyed roof elevations. Five-metre spacing is not five-metre accuracy.

Mountain evidence concerns sampled terrain around source peaks/ridges. Greenery evidence concerns exposed **mapped green-space terrain**, not measured trees/canopy or seasonal foliage. River groups are retained as intended samples but remain **unknown** because an independent compatible water-surface elevation is not available; no river-view claim is manufactured. Skyline requires at least two distinct supported building groups. Night geometry does not verify illumination.

The same intended scene set retains visible, blocked, excluded and unknown counts, including work left unevaluated. At least two distinct supported sample positions are needed for a card. Required selected scenery must have supporting components; unrelated candidates never fill an empty Top 3. Scores use fixed heuristic weights for preference match, sample support, composition, straight-line convenience, and a small astronomical-time adjustment. Unknown weather/crowds earn zero contextual adjustment. Open and framed preferences have different angular-spread optima, but neither establishes an unobstructed panorama. Solar azimuth/elevation comes from the existing NOAA approximation; a bounded directional horizon is supplementary and unknown beyond missing support. Atmospheric visibility and solar-disc appearance remain unknown.

## Candidate/access and source quality

The input has 582,255 candidate records, historically 572,453 distinct stored point geometries; they are not all eligible standing locations. Runtime retrieval uses 64 spatial strata, at most 160 initial representatives (configurable up to 500), conservative deduplication within 1 mm, and source lineage. It validates access/endpoint evidence before refining at most 20 spatially distributed observers, favouring supported high ground within directional strata. Target inventory covers the standing-location radius plus 10 km around the origin, with nearby quadrants and an outer band represented. Each actual candidate-to-target ray remains independently capped at 10 km. Twenty category/direction-stratified target groups and at most 10,000 rays/2,000,000 cells bound each query. This is a sampled search, not exhaustive ranking of all Seoul viewpoints.

Dedicated mapped footways/pedestrian areas provide positive experimental access evidence when unrestricted; `path`/`cycleway` needs explicit pedestrian evidence. Known private/closed tags override scenery, including coincident or containing restricted evidence. Uninterpreted access, schedules, gates and barriers remain exploratory-only. Steps, bridges, tunnels, nonzero layers and indoor endpoints cannot silently use terrain as their standing elevation. Missing route data is not disqualifying; route accessibility, wheelchair routes and current opening are not certified.

The seven source-invalid building flags remain intact. All seven original extents and dependent uncertainty regions are quarantined even where reprojected coordinates now pass GEOS. The adapter also masks recorded overlap discrepancies and any building bases depending on them. The selected building-distribution envelope gap is geometrically outside Seoul, at least 316.42 m away; this proves only selection-domain placement, not real-world detection completeness.

`reports/prototype/osm-recovery.json` records a bounded, network-free recovery from the already acquired PBF: 26 selected ways and all 2,593 required nodes, including two nested relations. Fifteen non-administrative problematic objects were localized outside Seoul; three can reconstruct valid geometry, including holes. All 46 original diagnostic IDs remain excluded; no normalized geometry, roof raster or readiness flag was rewritten. Thirty-one administrative diagnostics remain retained globally. Their names alone are not used as spatial proof.

## Storage, privacy and optional AI

The mandatory total is **20,000,000,000 decimal bytes**, including the whole checkout/data, partial files, staging, dependencies, caches and artifacts. The shared acquisition `Budget` supplies the same global writer flock, inode-deduplicated accounting, at least 25% uncertain-peak margin, 16 MiB checkpoint allowance, 8 GiB free-space floor and 4 GiB aggregate staging ceiling. Legacy 20 GiB and larger configuration/environment limits are rejected.

The prototype further lowers effective writer headroom to the measured startup footprint plus its 4,000,000,000-byte additions cap. Image cache is capped at 250 MB; query cache is memory-only, map derivatives are not created, and browser/log artifacts stay under 100 MB. The final browser dependency allocation is 900 MB including retained Chromium/Firefox archives, expansion and local native dependencies. The initial 400 MB Chromium plan correctly refused before extraction; a bounded Firefox fallback was added after Chromium could not use its sandbox on this host. These are suballocations inside the same limits, not extra storage. No acquired inputs or previous outputs are deleted. Browser archives remain recoverable, and all task-created temp/cache locations are accounted.

This is application-level enforcement, **not an OS quota**. Statvfs/free-space checks and a shared lock cannot guarantee safety against unrelated writers. No quotas, mounts, security settings or system services were changed.

Exact user origins are not persisted or sent to AI. Origin-dependent distance, score and description are retained only under a server-issued anonymous `HttpOnly; SameSite=Strict` session cookie (also `Secure` on HTTPS). The in-memory registry stores a hash of the random token and expires after one hour; it holds at most 256 sessions. The configured global view limit remains 60 entries across all sessions. Expiry, restart or eviction requires repeating the query. Different sessions cannot overwrite or retrieve each other's context for the same stable geographic view ID. Public geometry image-cache keys remain unchanged and contain no user origin. Diagnostics use deliberately selected public-source origins. Provider payloads contain validated evidence references rather than untrusted source names/free text. Source files and credentials cannot be retrieved through static URLs.

See [prototype-providers.md](prototype-providers.md) and `.env.example` for actual OpenAI Responses/Image adapters, explicit model IDs, verified pricing, durable daily spending reservations, image-job recovery/coalescing and cache keys. Paid calls default off. A key alone is insufficient: enablement, fresh explicit price configuration, nonzero authorized daily spending and an unset kill switch are required. Failed/ambiguous billable requests retain their reservation. Text may choose only validated evidence IDs/focus enums and cannot alter geometry, eligibility or rank. Deterministic descriptions and labelled geometry schematics remain available regardless of provider status. AI illustrations are never visibility evidence.

## Validate, reproduce and resume

```bash
bash scripts/prototype/python.sh scripts/prototype/check.py
bash scripts/prototype/python.sh scripts/prototype/run.py validate --report
bash scripts/prototype/python.sh scripts/prototype/benchmark.py
bash scripts/prototype/python.sh scripts/prototype/report.py
```

Fixture tests are synthetic. Run test writers under the shared reservation using `scripts/prototype/check.py` (documented below) for routine repeat validation; the runner invokes the actual pytest fixture suite. Benchmark checkpoints are implementation-version-aware and skip completed districts only for an identical version. Rate limiting is real: if it reports a rate-limit checkpoint, wait the stated interval and rerun the same command. It does not widen radii or change evidence thresholds.

Browser dependencies/tests are optional development tooling and never part of application startup. Reproducible setup and the browser actually used on this host:

```bash
bash scripts/prototype/python.sh scripts/prototype/browser_setup.py
bash scripts/prototype/python.sh scripts/prototype/browser_native.py
bash scripts/prototype/python.sh scripts/prototype/firefox_setup.py
bash scripts/prototype/python.sh scripts/prototype/browser_native.py --firefox
bash scripts/prototype/python.sh scripts/prototype/browser_run.py --engine firefox --base http://127.0.0.1:8000 --origin 126.81774514857614,37.56567741795218
```

Fetchers reuse verified archives and enforce expansion limits. The runner uses `browser_check.cjs`, counted temporary profiles and small retained screenshots. Chromium could not use its sandbox in this container; no sandbox was disabled. Firefox completed the checks using default security settings. Browser artifacts stay under `data/prototype`; small aggregate reports belong in `reports/prototype`.

The fresh execution summary, district outcomes, exact commands, browser availability, measured timings and remaining blockers are recorded in `reports/prototype/validation.md` and its linked JSON reports. Historical acquisition tests are not counted as tests executed during this task.

## Deployment gate

No public service was deployed. Before publication, record a review of the actual GBA conversion/building derivatives, ODbL obligations for displayed vectors/derived databases, terrain terms, attribution and output licence compatibility. Noncommercial use and an experimental disclaimer do not clear that gate. Surrounding terrain and water elevation remain data limitations independently of publication permission.

After permission and licence review, an HTTPS reverse proxy would need a deliberately configured allowed origin/host, trusted-proxy/client-rate policy, a read-only source mount, writable capped runtime/cache directories, one geometry worker, request-size/time limits and durable spending state. The current localhost-only host policy intentionally requires review before that change. No heavyweight container build is needed for local validation.
