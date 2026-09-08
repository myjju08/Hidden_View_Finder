# Seoul point visibility

A small Python package for **inverse visibility of one explicit 3D point**, using a prepared 2.5D terrain/building surface and one in-process `osgeo.gdal.ViewshedGenerate` call. Real Seoul contours, spot elevations and 2D building footprints were acquired and validated locally. **Datasets, prepared rasters, spatial indexes and map outputs are excluded from Git.** This repository contains code, acquisition scripts, tests and aggregate benchmark reports. See [the Korean overview and reproduction commands](../README.md), [data provenance](data-sources.md) and [real-data measurements](benchmarks.md). Synthetic fixtures and benchmarks are separately labelled; neither computational benchmarks nor source-model comparisons establish field visibility accuracy.

## Install and run

Tested with Python 3.12, Ubuntu 24.04 and **native GDAL 3.8.4 plus matching Python bindings**. Do not combine a different GDAL wheel, Rasterio's bundled GDAL, and the system bindings. Dependencies are pinned in `requirements.lock`; the package uses GDAL/OGR directly and does not need Rasterio, Fiona, a GPU or a database service.

```bash
# Ubuntu 24.04; install the native library before its matching Python bindings.
apt-get install --no-install-recommends \
  python-is-python3 python3-venv python3-dev build-essential libgdal-dev
bash scripts/install.sh
.venv/bin/python -m pytest -q

# Fictional 24 km square; sufficient surrounding coverage for these queries.
.venv/bin/seoul-visibility synthetic --output data/demo --size-m 24000
.venv/bin/seoul-visibility inspect data/demo --output reports/demo-inspection.json
.venv/bin/seoul-visibility query data/demo/manifest.json \
  --lon 127.00002829110686 --lat 37.54954050092772 \
  --height 120 --height-reference agl --radius 5000 --output data/demo-result.tif
.venv/bin/seoul-visibility benchmark data/demo/manifest.json \
  --output reports/my-benchmark --runs 30 --radii 1000 3000 5000 10000
```

Outputs use new paths: preparation and export refuse to overwrite existing sources or unrelated files. Commands return JSON; invalid inputs return a nonzero exit status. Run `python examples/usage.py` with the local environment for a small Python example. `seoul-visibility ... --help` describes each command.

The development environment was inspected before installation: about 42 GiB free, 32 CPUs in the process affinity, and approximately 231 GiB available host RAM (cgroup limits also measured). Native installation reserved 2 GiB peak and an 8 GiB free-space floor; the project-local GDAL/pyproj environment added roughly 52 MiB (the native OS installation reported a further 423 MB), sharing the pre-existing scientific Python packages. The install script instead supports a fresh isolated environment and reserves its installation footprint. Full machine details are in the benchmark JSON.

## Python API

```python
from seoul_visibility import TargetPoint, VisibilityEngine, State

with VisibilityEngine.from_manifest("data/demo/manifest.json") as engine:
    result = engine.visible_from_target(
        TargetPoint(127.00002829110686, 37.54954050092772, 120.0, "agl"),
        radius_m=5_000, eye_height_m=1.7, resolution_m=5,
        curvature_coefficient=6/7, use_cache=True,
    )
    visible_cells = result.states == State.VISIBLE
    sample_lonlat = result.sample_coordinates(max_points=100, seed=42)
    shortlist = engine.check_observers(
        TargetPoint(127.00002829110686, 37.54954050092772, 120.0, "agl"),
        sample_lonlat[:10], curvature_coefficient=6/7,
    )
```

`VisibilityResult` holds a compact immutable uint8 raster, CRS, GDAL-order affine transform, bounds, resolution, timing breakdown and metadata. Metadata includes requested and effective target coordinates, absolute elevation, datum declaration, eye height, radius, curvature, backend/version/mode, data version, coverage/quality, cache status and limitations. No coordinate list, polygonization, GeoJSON or display is created in the timed query path.

| State | Value | Meaning |
|---|---:|---|
| blocked | 0 | Evaluated eligible observer, native raster model says blocked |
| visible | 1 | Evaluated eligible observer, native raster model says visible |
| excluded | 2 | Ineligible observer, outside radius/output boundary, occupied, or rejected by candidate mask; **not** a physical blockage claim |
| unknown | 3 | Cannot classify; **not** blocked or visible; GeoTIFF NoData value |

The strict dense API rejects unknown computational coverage rather than emitting confident results behind it. Sparse checks return unknown per ray and excluded for invalid or occupied observer endpoints, preserve input order, and attach reasons. Sparse checks use the independent Python reference and are intended for small shortlists (maximum 10,000 entries, streamed by the caller beyond that), not dense classification. Their closed-column geometry can disagree with GDAL's approximation.

A candidate mask is a boolean NumPy array covering the entire prepared grid, or `CandidateMask(values, transform, crs)` for explicit alignment verification. Only its relevant window is copied/hashed. Candidate and Seoul masks filter output **after** obstruction computation. They never remove intermediate terrain or buildings. A candidate mask does not certify public access unless the caller supplied appropriate access data.

## Height and geometry contract

* `DTM(x,y)` is bare-earth elevation in the manifest's documented vertical reference; `S(x,y)` is the maximum of terrain and modeled absolute building roofs.
* Ground observers have elevation `DTM + eye_height_m`. Open-ground eligibility requires occupancy zero **and** `S == DTM` within 1 micrometre. The engine checks this invariant. Adding trees or elevated platforms requires changing this endpoint model, not just raising unoccupied surface cells.
* An AGL target is `DTM(source cell) + height_m`, **not roof + height**. An absolute target must provide `TargetPoint(..., "absolute", vertical_reference="exact manifest value")`. The explicit matching declaration guards against accidentally using GPS ellipsoidal heights with orthometric terrain. No vertical conversion is implemented; horizontal reprojection is not a datum conversion.
* A target on or above a modeled roof is supported. Below-surface targets are rejected (1e-6 m source-validation tolerance). No target building or adjacent footprint cells are removed, and target elevation is never raised to make a facade/indoor point valid.
* Reciprocity gives GDAL observer position = target, observer height = `z_target - S(source)`, target height = human eye height, input = unchanged `S`. Eye height is **never added to obstacle tops**. This represents visibility of the specified point only, not an entire building.

Transforms use GDAL order `(x_origin, pixel_width, 0, y_origin, 0, -pixel_height)`. Origins are upper-left **pixel corners**; samples and dense observer locations are at pixel centers. Public coordinates are WGS84 **longitude, latitude**, transformed with `always_xy=True`. Target selection uses the containing half-open pixel and snaps to its center. A measured GDAL 3.8.4 probe gave identical results for five different positions within one cell and changed results in an adjacent cell. Requested and effective XY/lon/lat are both reported; no sub-pixel dense accuracy is claimed. Distances and the inclusive radius mask use projected horizontal distance from the **effective target center**. Grid spacing is not map accuracy or guaranteed alley resolution.

The curvature coefficient is explicit: default `6/7` approximates visible-light refraction, `0` is flat, `1` has curvature without refraction. GDAL corrects heights by `k*d²/D`, with `D = 2 * CRS ellipsoid semi-major axis` (12,756,274 m for EPSG:5186). The independent reference uses equivalent symmetric ray clearance:

```
clearance(t) = (1-t)*z_start + t*z_end - S(cell) - k*L²*t*(1-t)/D
```

Reference obstacles are closed vertical columns. It intersects the ray with each traversed cell's **whole interval**, tests the quadratic minimum and its endpoints, includes zero-length edge/corner contacts and both endpoint cells, and allows contact within 1e-6 m. Grid-boundary tolerance is 1e-9 pixel. These conventions prevent sampling from skipping a thin blocker and preserve reciprocity. GDAL `GVM_Edge/GVOT_NORMAL` instead propagates an interpolated raster horizon, with special treatment near its source; native comparisons do not implement our reference tolerance. Neither backend mode nor this grid is asserted to be a guaranteed visibility bound.

## Inspect, plan, prepare real inputs

```bash
.venv/bin/seoul-visibility inspect /path/to/local/inputs --output reports/source-inventory.json
# Optional for Korean SHP whose encoding declaration is missing/wrong:
.venv/bin/seoul-visibility inspect /path/to/buildings.shp --encoding CP949
# Copy and fill the deliberately incomplete template after inspection.
.venv/bin/seoul-visibility plan examples/my-area.json
.venv/bin/seoul-visibility prepare examples/my-area.json
```

`examples/config.template.json` contains placeholders, not invented Seoul field names. `examples/manifest.synthetic.json` is a working manifest example referencing the generated `data/benchmark` fixture without copying its rasters. Paths are resolved relative to the config file. Start with a small area, inspect its quality/validation manifest, then increase extent within the plan's storage estimates. `examples/make_fixture_inputs.py` creates clearly fictional raw inputs and a runnable configuration for exercising all three stages without real files. Its defaults refuse an existing fixture; pass `--data-root data/fresh-fixture` to create another.

```bash
.venv/bin/python examples/make_fixture_inputs.py
.venv/bin/seoul-visibility inspect data/raw-example --encoding CP949
.venv/bin/seoul-visibility plan data/fixture-config.json
.venv/bin/seoul-visibility prepare data/fixture-config.json
.venv/bin/seoul-visibility query data/prepared-example/manifest.json \
  --lon 127.00002829110686 --lat 37.54954050092772 \
  --height 25 --height-reference agl --radius 500
```

The actual run is recorded in [reports/preprocessing_fixture.json](../reports/preprocessing_fixture.json): all commands succeeded, all source hashes remained unchanged, and the 300 × 300 output had complete fictional coverage.

Inspection scans feature counts, layer bounds/CRS, geometry types, fields/missingness/zeros/ranges, geometry Z, SHP sidecars, encoding metadata, raster NoData and units. It suggests field candidates without choosing them. Filesystem dates are reported separately from source dates, which remain unknown unless documented. DXF inventories distinguish CAD `Layer` values inside OGR's entities layer. Select `cad_layers` explicitly; a DXF entity is not automatically a contour or spot elevation.

Required real-data decisions are:

1. Actual source CRS for each layer (unknown or conflicting CRS fails), documented metre height units and compatible vertical reference. EPSG:5186 is the proposed internal projected CRS, not a replacement assigned to unknown source coordinates.
2. Verified bare-earth raster **or** selected contours/spots with an explicit elevation field or genuine geometry Z. Raster provenance needs `bare_earth_verified: true`. A 5 m source raster cannot be relabeled a 2 m refinement. Prepare a separate product from suitable inputs for 2 m queries.
3. Building measured **AGL** height field with `height_is_agl: true`; optional reliable absolute base field. Floor estimates require an explicitly chosen above-ground floor field, `floors_are_above_ground: true` and `floor_height_m`. Zero/missing height is unresolved unless an approved estimate applies. Optional fixed missing-height imputation requires explicit positive height and justification; it receives an estimated quality flag.
4. Building **survey coverage**, supplied as a rectangle in internal CRS and optionally an exact `coverage_boundary` polygon. Terrain also accepts `coverage_boundary`; cells not fully covered remain unknown. Footprint extents do not establish absence of buildings outside those footprints. Supply surrounding data wherever a line of sight may leave Seoul. Optional Seoul boundary is a separate final output mask.
5. Data root, processing extent, source dates/provenance, storage policy and interpolation support. A publication scale such as 1:1,000 or 1:5,000 does not certify raster accuracy.

Preparation performs no downloads and preserves source datasets. A verified terrain raster is horizontally warped once with bounded GDAL memory. A separate source-validity minimum warp and one-cell expansion of unknown support prevent bilinear interpolation from silently filling source holes. Otherwise contour lines are sampled at controlled spacing; the default preserves vertices, while explicit `contour_sampling="regular_arclength"` bounds density on constant-elevation contours and retains both endpoints. Original vectors are retained in either case. A SQLite RTree indexes samples. Each tile triangulates only its bounded halo. Limits cap global sample count and local triangulation size; conflicting duplicate XY elevations fail instead of being averaged. This **unconstrained sampled-contour TIN is an approximation**. It leaves NoData outside supported local convex hulls or where a triangle edge exceeds the configured limit. It records seam comparisons and held-out spot residuals where available; held-out validation points are included in the final DTM. Validation excludes all held-out points from its TINs and evaluates a bounded deterministic subset (`max_validation_spots`, default 500; seed recorded). No contour painting, zero filling, dense citywide distance matrix or global unbounded TIN is used.

Buildings are normalized once into an indexed GeoPackage. Multipart polygons/holes are retained; invalid geometry is repaired with counts or rejected; empty/nonpolygonal irreparable features fail. Exact duplicates are removed deterministically. A base elevation defaults to the median valid all-touched DTM cells under each footprint; absolute roof is base plus usable AGL height. A reliable configured base attribute takes priority. Terrain variation above a configurable slope threshold or a roof below local terrain creates a conflict flag. Full footprint support is needed; partial terrain support remains unresolved. Spatially filtered burns are ordered by absolute roof elevation so overlapping roofs retain their **maximum**, independent of source order. Unknown overlapping buildings stay unresolved even if another roof is known.

Published height estimates require `height_is_estimated: true` and a documented `height_estimation_method`, producing `APPROXIMATE` results. The optional `base_estimation_method="maximum"` additionally requires `base_estimation_justification`: when a reliable absolute base is absent, it places the roof at the highest footprint DTM plus unchanged AGL height. This explicit roof overestimate may falsely block rays and is not a guaranteed bound. Large terrain relief remains counted; unknown terrain/heights and conflicting supplied absolute bases still fail. The real pilot's median-base rejection and subsequent explicit maximum-base run are both retained in `reports/seoul-pilot-*-validation.json`.

Losslessly compressed tiled products are Float32 DTM/surface and Byte occupancy/quality, with an optional output mask. Conservative `all_touched` footprint rasterization can inflate footprints and falsely close courtyards/narrow gaps; roof columns are not an exact building model. Terrain, surface, occupancy, coverage, quality and output mask share one fixed grid aligned to origin `(0,0)`.

| Quality bit | Meaning |
|---:|---|
| 1 | Terrain valid |
| 2 | Full cell lies within documented building survey coverage |
| 4 | Approved estimated building height |
| 8 | Unresolved height or building terrain support |
| 16 | Terrain/roof slope conflict requiring review |

A dense query requires bits 1 and 2 throughout its required computation region and rejects bits 8 or 16 there. Estimated heights yield an `APPROXIMATE` result with provenance. Query-time terrain imputation is not implemented. For the inspected **GDAL 3.8.4 / GVM_Edge / north-up square grid**, required coverage is the target-centered radius plus a conservative one-cell halo, within a rectangular read window. The native predecessor stencil only propagates heights from closer cells; source inspection and 180 outside-radius perturbation comparisons establish the dependency region. [Proof and measurements](../reports/gdal-radial-dependencies/probe.md) are separate from any claim of physical visibility accuracy. Unknown unused corners receive finite `1e9` padding only in the temporary MEM input and are excluded from output; original NoData/quality remain unchanged and padding counts are reported. Unknown cells inside the required region still reject, regardless of candidate masks. Unreviewed versions/modes fall back to full rectangular validation. The read rectangle itself must remain inside the prepared raster.

Versioned manifests record aligned grids, source/sidecar SHA-256 fingerprints, processing settings, GDAL version and terrain/building validation. Stage completion is persisted in a tool-owned hidden preparation directory. Completed stages are reused on restart; an interrupted stage is rebuilt. Only a final atomic directory rename publishes a `ready` manifest. No unfinished dataset is advertised as ready. Old products are not overwritten; use a new output directory for changed inputs.

## Resources, caching and limitations

Defaults are 20 GiB total project data, 8 GiB minimum free disk, 4 GiB temporary artifacts within the total, and a 1 GiB disk-cache policy cap. **Disk result caching is disabled (zero bytes)**; the implemented result cache is a 128 MiB RAM LRU, and the GDAL data block cache is 64 MiB. Planning includes raw sources (including configured external files), lossless output upper estimates, indexes, journals, staged copies and an optional environment-installation allowance. Preparation rechecks free space and staged bytes. Nothing deletes sources to recover space. Keep exports within the declared data root or supply an explicit encompassing export budget root so accounting includes sibling raw/processed products.

Dense queries read only local windows and create local arrays with broadcast 1D coordinate vectors. They never reopen source vectors, reproject a city, interpolate terrain, rasterize footprints, or independently ray-trace every observer. A finite positive radius is mandatory. Default radius is 5 km; the interactive limit is 10 km. Larger calls need `offline=True` and pass a measured RAM preflight reserving half the available process/cgroup memory. There is one native job at a time per process; an engine rejects calls from another thread. No worker pools or nested BLAS/native thread pools are started. Stream multi-target results rather than retaining all rasters.

Cache identity includes manifest/data fingerprints and prepared file signatures, backend/version/mode, requested/effective target and resolved elevation, grid/resolution, radius, eye height, curvature, candidate-window content and quality policy. It never rounds target coordinates/heights into a shared cache key. Changing prepared files while an engine is open invalidates its cache and requires reopening. Results are backed by immutable bytes to prevent caller writes from contaminating cache hits. Raw source changes require a new prepare/version; query-time vector scanning would defeat the prepared-data contract.

Trees, balconies, signs, temporary obstacles, atmospheric visibility, bridge decks/overhangs and indoor viewpoints are outside this model unless explicitly represented. Visibility says nothing about scenic value or public accessibility. No coarse-to-fine pruning is implemented: 5 m visibility is not a safe hard filter for a separately prepared 2 m model. Optional vector output and rendering remain outside this package.

## Validation and performance

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/validate_backend.py
.venv/bin/seoul-visibility benchmark data/demo/manifest.json \
  --output reports/my-block-cache-profile.json --runs 30 --profile-block-cache
```

See [reports/benchmark_report.md](../reports/benchmark_report.md), the JSON/CSV reports, and `reports/backend_validation.json`. Native hard regressions cover flat ground, clear wall shadows, low obstacles without eye-height inflation, and curvature horizons. The independent reference covers grazing heights, roofs and adjacent cells, ridges, thin/corner blockers, endpoint contact and reciprocity. Integration tests cover missing coverage/heights, boundaries, invalid targets, axis order, snapping/offsets, radius edges, unavailable resolution, footprint repair/holes/overlaps, caches, budgets, atomic resume and cleanup.

The diagnostic native/reference comparison recorded **124 false-visible and 14 false-blocked** classifications across deliberately challenging synthetic samples. They are disagreements relative to the documented closed-column reference, not estimated real-world error rates. Simple unambiguous cases remain hard tests; above-roof/source-cell intervals, corner contacts and near-horizon cases demonstrate native approximation limits. Do not treat these counts as validating native accuracy on Seoul.

Benchmarks distinguish creation/inspection, process launch/import, opening, warm **uncached** viewsheds, exact cache hits, read/validation, native computation, masking, sparse reference checks and export. They use spatially distributed targets and 30 timed runs per radius, with RSS and disk measurements; targets lacking required coverage are explicitly skipped. Filesystem caches are uncontrolled and never called cold disk; no privileged flushing or JIT is used. The 5 km/5 m design goals are measured goals, not a promise on other machines or terrain. Real Seoul runs and limitations are documented in [the real-data benchmark guide](benchmarks.md). A separately prepared local 2 m Seoul product is unavailable. Report paths written as code instead of links are local generated artifacts excluded from publication.

## Engineering references

Installed API behavior was tested against GDAL 3.8.4 and its version-specific implementation, alongside these primary references:

* [GDAL algorithm API](https://gdal.org/en/stable/api/gdal_alg.html) and [Python utilities](https://gdal.org/en/stable/api/python/utilities.html).
* [GDAL viewshed options, height offsets, NoData limitation and curvature formula](https://gdal.org/en/stable/programs/gdal_viewshed.html).
* [GDAL 3.8.4 viewshed implementation](https://github.com/OSGeo/gdal/blob/v3.8.4/alg/viewshed.cpp).
* [GDAL grid](https://gdal.org/en/stable/programs/gdal_grid.html) and [rasterize](https://gdal.org/en/stable/programs/gdal_rasterize.html).
* [pyproj Transformer and always_xy](https://pyproj4.github.io/pyproj/stable/api/transformer.html).
