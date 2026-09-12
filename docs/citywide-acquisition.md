# Seoul citywide geographic inputs

`scripts/data/citywide.py` extends the repository's pinned terrain acquisition,
strict building HTTP-range reader, and terrain TIN builder. It does not compute
recommendations, viewsheds, travel routes, tiles for a basemap, or generated images.
The administrative standing-location polygon is Seoul OSM relation 2297418.
The obstruction support is that polygon buffered by **10,000 m** in EPSG:5186;
the terrain interpolation support adds **1,000 m**. Travel distance is a separate
future straight-line filter. The 5 m raster convention does not imply 5 m accuracy.

## Commands

With an existing compatible GDAL/Python environment:

```bash
PYTHONPATH=src:. python scripts/data/citywide.py inspect
PYTHONPATH=src:. python scripts/data/citywide.py plan
PYTHONPATH=src:. python scripts/data/citywide.py boundary
PYTHONPATH=src:. python scripts/data/citywide.py plan --online
PYTHONPATH=src:. python scripts/data/citywide.py all --candidate-max-bytes 805306368
PYTHONPATH=src:. python scripts/data/citywide.py validate
PYTHONPATH=src:. python scripts/data/citywide.py resume
```

For a bounded real-data smoke check or an independent source stage, use
`normalize --only terrain` or `normalize --only osm`. Normalization also supports
`--only candidates`, `--only coverage`, and `--only package` after their inputs
have validated.

The measured full-city candidate bound is 648,667 before building/water
exclusions. The initial 256 MiB artifact allowance proved too small for a
conservative plan. Use the measured 768 MiB allowance for a clean citywide run:

```bash
bash scripts/data/python.sh scripts/data/citywide.py all --candidate-max-bytes 805306368
```

This resource-only override is persisted in run state and package configuration;
`resume` reuses it. It changes no source version, geographic extent, 20 m sampling,
or total/staging/free-space ceiling. Every candidate write still has a per-file
SQLite bound and a shared peak reservation. Interrupted candidate attempts are
retained through the separately budgeted rebuild.

`plan` defaults to zero network. `plan --online` fetches bounded Parquet metadata
after the boundary is available; it does not fetch feature row groups. `boundary`
makes one cached Nominatim city request and inspects the current response as a
separate source record. The old pinned response fingerprint is preserved.
The country PBF is a dated snapshot with a verified Geofabrik MD5, plus a locally
computed SHA256. Date changes require inspecting and versioning the configuration.

This execution environment initially lacked GDAL, PyProj and pyosmium. An optional
**Ubuntu 24.04 / Python 3.12 only** bootstrap downloads the native dependency list
reported by the installed APT metadata, checks its published SHA256 and MD5 values, and
extracts packages into the accounted project directory without installing them
into the operating system or executing maintainer scripts. Public PyPI wheels
are SHA256-checked. NumPy1.26.4 and SciPy1.15.3 are paired with Ubuntu's GDAL3.8.4
bindings; the existing shared environment is preserved.

```bash
python scripts/data/bootstrap.py              # bounded metadata plan
python scripts/data/bootstrap.py --execute    # optional local dependency setup
bash scripts/data/python.sh scripts/data/citywide.py all --candidate-max-bytes 805306368
bash scripts/data/python.sh scripts/data/citywide.py validate
bash scripts/data/python.sh scripts/data/citywide.py resume
```

The wrapper places dependencies, temporary files, caches, GDAL data, and native
libraries on explicit local paths. It disables GDAL worker pools and PROJ network
grids. Do not use the legacy `scripts/install.sh` for the citywide execution.

## Storage and recovery

The shared `Budget` accounts the **entire checkout**, including existing GIS,
`.git`, code, hidden staging, partial downloads, journals, caches, logs, dependency
archives and installations. Additional data roots must be explicitly configured;
unrelated directories are not searched. Hard links are counted once in the total,
while a staging alias still counts against the temporary ceiling. Both logical
and allocated bytes are measured, and the larger is charged per inode.

The immutable upper limits are **20,000,000,000 total bytes**, **4 GiB temporary**,
and an **8 GiB free-space reserve** per receiving filesystem. A lower total or
temporary limit is allowed; the former 20 GiB value is rejected. Uncertain stage
peaks receive at least25% extra margin. Downloads also reserve16MiB for filesystem
allocation beyond logical EOF; small checkpoint space is protected separately.
On this Linux filesystem, downloads and ZIP members use exact bounded
`fallocate(FALLOC_FL_KEEP_SIZE)` allocation before payload writes. This prevents
speculative allocation from repeatedly doubling while preserving the true partial
download length. A filesystem that cannot provide this bound stops the affected
writer before payload writes. Earlier tiny fixture runs outside the checkout have
a conservative5MiB additional footprint allowance; later tests use accounted staging.
Reservations are protected by a nonblocking advisory `flock`, with the lock inode
retained across runs. A dead process releases its lock; a live worker is never
deleted or replaced. Native SQLite outputs have page ceilings, and raster workers
have per-file limits plus bounded simultaneous outputs and monitored lag margins.

This is an application-enforced byte policy, **not an installed OS quota**.
Filesystem polling cannot prevent unrelated users from consuming shared disk
space between checks. Stages whose simultaneous outputs cannot be bounded are
refused. The startup filesystem had approximately26.04decimalGB free, permitting
about17.45GB growth after the8GiB reserve before other margins;20GB was never an
additional download allowance.

Downloads use bounded streams, strong object validators for resume, strict206
responses and Content-Range validation. A completed `.part` can recover only after
source identity and content validation. Source hashes are never changed to accept
new bytes. ZIP extraction rejects traversal, links and special entries, verifies
declared and actual expansion, and retains source archives. Cleanup is restricted
to recorded task-owned intermediates with verified recoverable successors, with
deletions recorded. Interrupted building row groups commit their feature data and
checkpoint together. Changes to settings produce a separate normalization version.

## Outputs and interpretation

Development inputs and state live in `data/citywide/`, which is ignored by Git.
The versioned `packages/` directory contains ordinary relative-path geographic
files, source and licence records, configuration, and SHA256 manifests. Publication
uses a same-filesystem directory rename after validation; hard links avoid an
unnecessary duplicate on disk. A copied package is standalone. Raw recovery sources
and the local Python runtime remain separate from the compact geographic subset.

The package contains the administrative and requested support geometry, normalized
terrain vectors, indexed building footprints/heights, OSM paths and context,
standing candidates, and coverage/quality geometry as available. Source IDs,
height missingness, estimated-height flags, uncertainty and mapped access tags are
retained. Height above ground, terrain elevation, absolute roof elevation, and
observer eye height remain distinct. No horizontal transformation is described as
a vertical-datum conversion.

OSM relation assembly reads the complete permitted country snapshot before spatial
selection, keeping required nodes, multipolygon members and holes. Unassembled or
invalid objects are explicitly reported; no missing tag becomes false or verified.
Candidate IDs derive from original way IDs and20m chainage. Explicit pedestrian
areas and squares with pedestrian permission also use a stable20m projected grid.
Candidates are confined to Seoul and excluded by mapped building/water geometry.
Park interiors are not assumed walkable. Bridges, tunnels and elevated layers carry unresolved observer
elevations. Candidates are not claimed scenic, visible, currently open or field-verified.

The completed run retained **582,255 candidate records**: 579,891 path samples and 2,364 explicit pedestrian-area grid samples, in a 443,691,008-byte indexed GeoPackage. The 648,667-point planning upper bound is separate from this measured result. There are 572,453 distinct stored point geometries: 9,023 coincident-geometry groups retain 9,802 additional source-linked records. Candidate IDs remain unique; differing access or elevation evidence is preserved instead of merging records. See [exact geometry duplicate audit](../reports/citywide/geometry-duplicates.json).

Access evidence remains unknown for 547,118 records; 33,703 record mapped permission and 1,434 permissive access, both unverified. All records remain unverified in the field and have unknown current opening status.

Coverage identifies 1,330 landmark-source objects inside Seoul and all 25 districts; three candidate records remain unassigned at microscopic boundary differences consistent with floating-point precision, with no snapping or inferred assignment. See [candidate planning and measurements](../reports/citywide/candidate-planning.json) and [district-boundary evidence](../reports/citywide/district-gap-inspection.json).


`acquisition_ready`, `normalized_ready`, `visibility_ready`, source-distribution
coverage, and deployment licence review are separate report fields. Successful
file integrity checks do not establish physical source completeness. Official
Seoul-only contours do not supply the surrounding terrain buffer. Terrain tiles
retain unsupported cells as NoData and their quality bits remain unset. The
existing engine's full-window coverage checks are not weakened.

`normalized_artifacts_complete` records successful publication of all normalized
inputs independently of unresolved geography. Unlocated incomplete OSM relations
and seven unrepaired building geometries keep `normalized_ready` false. Names
cannot establish where missing geometry lies. Source IDs, invalidity flags and
diagnostics remain preserved; successful file publication does not clear these
readiness gates.

See [current source review](citywide-source-review.md) for the older mixed-source
GBA conversion, official terrain alternatives, OSM obligations and public deployment
licence review. Weather/crowd integration remains optional and was not bulk acquired.
Missing crowd data is not evidence of quietness.

The executed terrain-density plan covered all 109 requested 5 km output tiles:
108 used 256×256-pixel processing windows, while `x39_y111` used 128×128 to stay
below the unchanged 100,000-sample query cap. Output cells remain 5 m and the
Seoul/support/halo contract is unchanged. Preparation completed 109 tiles:
33 validated compatible tiles reused and 76 newly prepared, in 7,496.66 seconds.
The shared 234,852,352-byte sample index and old tiles remain recoverable.
The final manifest is `data/citywide/prepared/67cd2654e14de3c0/manifest.json`.

Terrain coverage remains incomplete: 95.9068% of Seoul cell centres and 27.5127%
of requested support cell centres have valid terrain. Unsupported cells remain
NoData, and `visibility_ready=false`. Tile completion and 5 m spacing do not
establish complete coverage or 5 m accuracy. See the
[execution record](../reports/citywide/validation.md) and
[preparation checkpoint](../reports/citywide/preparation-interruption.json).

Small aggregate execution reports are tracked under `reports/citywide/`.
The validation report lists commands actually run and distinguishes passed,
failed, skipped and unrun checks. Fixture tests never download citywide sources.
