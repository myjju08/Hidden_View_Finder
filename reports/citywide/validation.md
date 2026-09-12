# Execution and validation record

This task ran against the existing checkout on 2026-09-11. GIS and runtime files
are retained under `data/citywide/`; no data or code was committed or pushed.
The latest machine-readable stage outcomes and commands are in
[acquisition.json](acquisition.json). A successful integrity check is separate
from coverage, public access, model accuracy, and field verification.

## Tests actually executed

All full-suite commands used the local compatible runtime and a fresh accounted
temporary directory:

```bash
bash scripts/data/python.sh -m pytest tests -q --basetemp=data/citywide/staging/full-tests-08 -p no:cacheprovider --junitxml=data/citywide/full-tests-08.xml
```

The same command with numbered paths `01` through `07` was run earlier.
Retained JUnit results are:

| Run | Passed | Failed | Errors | Skipped |
| --- | ---: | ---: | ---: | ---: |
| full-tests-01 | 396 | 2 | 0 | 0 |
| full-tests-02 | 399 | 0 | 0 | 0 |
| full-tests-03 | 409 | 0 | 0 | 0 |
| full-tests-04 | 429 | 0 | 0 | 0 |
| full-tests-05 | 451 | 0 | 0 | 0 |
| full-tests-06 | 456 | 0 | 0 | 0 |
| full-tests-07 | 473 | 0 | 0 | 0 |
| full-tests-08 | 518 | 0 | 0 | 0 |

The first two failures concerned the native building checkpoint primary-key
field and a fixture's unavailable SQLite spatial function. Both were corrected
before the later complete suites. The successful full suites emitted two
multiprocessing fork deprecation warnings. No skipped test is counted as passed.

Additional coordinated checks after run 04:

```bash
bash scripts/data/python.sh -m pytest tests/test_citywide_pipeline.py -q --basetemp=data/citywide/staging/pipeline-readiness-tests-20260911 -p no:cacheprovider
bash scripts/data/python.sh -m pytest tests/test_citywide_osm_distribution.py tests/test_citywide_pipeline.py -q --basetemp=data/citywide/staging/distribution-readiness-tests-20260911 -p no:cacheprovider
bash scripts/data/python.sh -m pytest tests/test_citywide_osm.py -q --basetemp=data/citywide/staging/osm-duplicates-tests-20260911 -p no:cacheprovider
```

These reported **11**, **15**, and **33** passed respectively. The last emitted
one GDAL default-exception-policy future warning. Fixtures simulate storage
pressure, HTTP failures, interruptions, invalid geometry and incomplete relations;
they never fill the actual disk or download citywide inputs. See the separate
[safety audit](safety-validation.md) for the mechanisms and historical limitations.

Coverage recovery tests subsequently reported **10 passed** and preparation
bound/NoData parity tests **7 passed**, each with one GDAL future warning. The
complete suite was then rerun using `full-tests-05` in the command above:
**451 passed**, zero failures/errors/skips, in 17.53 seconds, with the same two
multiprocessing deprecation warnings. This includes package interruption recovery,
input-arrival coverage invalidation, and the actual OSM tag-duplication regression.

The retained `full-tests-06.xml` then recorded **456 passed**, zero failures/errors/skips, with a 16.723-second JUnit suite duration. The subsequent complete `full-tests-07` run reported **473 passed**, zero failures/errors/skips, in 17.69 seconds, with two known multiprocessing fork deprecation warnings. Both JUnit summaries were checked directly before this update. No actual raster result is inferred from these synthetic tests.

After run 07, the audited preparation-migration/reuse checks reported **37 passed** and the preparation/window-planning checks **21 passed**. The definitive `full-tests-08` command above then reported **518 passed**, zero failures/errors/skips, in **18.99 seconds**, with the two existing multiprocessing fork deprecation warnings. Its retained JUnit summary was inspected directly (18.951-second suite duration). These test results do not certify completed raster reuse or citywide preparation.

## Real-data checks and recoveries

- The locally extracted native runtime passed a GDAL in-memory raster/NumPy
  write/read check: GDAL 3.8.4, NumPy 1.26.4, SciPy 1.15.3, PyProj 3.7.2.
- The official terrain archive matched its unchanged repository fingerprint.
  All 15 archive members and the expanded sidecars were inspected before use.
  Actual normalization retained 8,570 contours and 45,870 elevation points,
  with no missing elevations, duplicate source IDs, or invalid geometry.
- The strict-range building smoke stage exposed excessive tiny range reads.
  It was interrupted safely, retaining the committed row group and rollback
  state. Bounded physical-row-group prefetch and 1,024-row decoding completed
  all 46 selected groups on resume. Every committed range and the final local
  GeoPackage were validated; the whole remote object was not downloaded or hashed.
- The dated South Korea PBF matched Geofabrik's independently published MD5.
  Its local SHA-256 and parseable header were checked. A count-only preflight
  found 38,346,399 nodes, beyond the initial 25-million count cap. A bounded
  50-million-node probe completed, estimating 1,743,416,536 incremental memory
  bytes; the unchanged 2 GiB memory preflight remained mandatory.
- An interrupted read-only native PBF probe exited with signal 11. Future
  pipeline and bootstrap invocations disable core dumps. No project core was
  found, and the configured core-directory parent was absent in this container.
  The verified PBF remained intact.
- The first OSM normalization stopped on a duplicate public-space source ID:
  an actual square carried both `highway=pedestrian, area=yes` and `place=square`.
  Repeated classification matches now emit the same source geometry once;
  original tags remain intact and the merged-match count is recorded. Native
  fixture regressions passed. The 22,134,784-byte interrupted GeoPackage was
  preserved before the next attempt, rather than deleted or published.
- The separately downloaded 5,657-byte Geofabrik extraction polygon is valid
  and covers all three requested geometries, including the terrain halo. This
  is publisher distribution-domain evidence, not object completeness or an
  independently pinned polygon version for the dated PBF.

The initial source inspection recorded 14 incomplete country-boundary relations.
Final OSM normalization retained a validated **209,899,520-byte** GeoPackage with
**289,022 source-layer feature emissions**, including **239,880 path records**, plus
**46 quality rows**. A source object may appear in multiple layers. The receipt
records **36 unassembled relations**, **34 geometry issues**, **zero geometry
repairs**, and **19 merged duplicate classification matches**. All missing or
invalid source geometry remains explicit; successful index/format checks do not
resolve these completeness gaps.

Bounded inspection of the 22 additional relations found three label-only
relations, four containing only nested/subarea relations, and 12 whose declared
way members have unclosed endpoints. Three relations remain unresolved without
coordinate inspection. One problematic source way contains only one node.
Names and country-edge metadata do not prove the missing geometry is outside
Seoul or its support area. IDs, member evidence and probe timings are retained
in [osm-relation-inspection.json](osm-relation-inspection.json).

The completed building subset contains **435,438 footprints**, with **seven
retained invalid geometries** and **zero unresolved source heights**, across all
46 selected row groups. Building heights remain estimated metres above ground;
these counts do not establish complete detection of real buildings. The seven
unrepaired building geometries are now an explicit `normalized_ready` gate,
separate from successful publication of the inspected source files.

Candidate generation completed with **582,255 indexed records** in a
**443,691,008-byte** GeoPackage: **579,891 path-chainage samples** and **2,364
explicit pedestrian-area grid samples**, at the unchanged 20 m spacing. The
receipt reports zero invalid candidate geometries and zero duplicate generated
candidate IDs.
The **648,667-point planning upper bound** is separate from this measured count.
The initial 256 MiB attempt was interrupted before its cap, retaining its
84,287,488-byte partial; the validated retry used a separately reserved 768 MiB
artifact cap inside the unchanged shared storage ceiling. The retry took
1,816.078 seconds and its invocation recorded 262,479,872 bytes peak RSS.
See [candidate-planning.json](candidate-planning.json) for estimates, reservations,
interruption history, source hashes and measured outputs.

A later exact stored-geometry audit found **572,453 distinct candidate point
geometries**, with **9,023 coincident-geometry groups** and **9,802 excess records**.
The 582,255 count therefore refers to source-linked candidate records, not unique
XY locations. Different records can retain different access or structure-elevation
evidence; none was merged. This is distinct from duplicate candidate IDs and
from the three district-boundary predicate cases. The overall OSM boundary layer
also has two excess exact geometries; that is not evidence of a missing Seoul
district. All 46 quality records deliberately share an explicitly labelled
unlocated marker, not a claimed observed object location. See
[geometry-duplicates.json](geometry-duplicates.json).

A read-only full evidence scan counted **547,118 unknown access statuses**,
**33,703 mapped-permission statuses**, and **1,434 permissive-access statuses**.
These are unverified evidence classes, not public-access confirmations or an
exact count of absent tags. **12,250 records** carry unsupported-structure
observer-elevation uncertainty; **570,005** still require terrain elevation. All
582,255 records retain `field_verified=false` and unknown current opening.

Coverage inspected **109 support-area grid cells** and **all 25 Seoul districts**.
All 582,255 candidates are inside the recommendation polygon; three remain
unassigned to a district. An indexed check inspected only 776 candidates near
502 microscopic polygon-difference fragments and found those three source-way
samples within approximately 1.8e-11 to 2.5e-11 metres of Geumcheon-gu. This is
consistent with floating-point boundary predicates; no point was snapped or
assigned by inference. The district-union gap totals about 0.000010572 square
metres. **1,330 landmark-source objects** are grouped inside Seoul, with zero
unassigned. See [district-gap-inspection.json](district-gap-inspection.json).

The first complete normalized-input package snapshot
`seoul-20260911-v1-845efa4b2851` contains **15 listed
artifacts** plus its manifest, totalling **866,156,649 bytes**. It includes the
Seoul/support extents, terrain, buildings, OSM context, candidates, coverage,
Geofabrik extraction-domain polygon, source receipts, configuration and licences.
An actual offline validation completed in **9.202 seconds** with all package
checksums, sizes and supported schemas passing. A separate bounded inspection
confirmed relative paths, sizes and the five normalized GIS hardlinks.

The package correctly separates `acquisition_ready=true` and
`normalized_artifacts_complete=true` from `normalized_ready=false` and
`visibility_ready=false`. The final source package snapshot
`seoul-20260911-v1-6abf3cc88682` totals **866,156,702 bytes** and explicitly
records the seven unrepaired building geometries; source GIS bytes remain unchanged.
Those geometries, unlocated OSM geometry gaps, missing compatible terrain outside
the official Seoul source, and deployment licence review remain open.
These outputs do not establish scenic quality, visibility, current opening or
field verification. The historical cleanup-order deviation and incomplete early
accounting remain disclosed in the [safety audit](safety-validation.md); later
passed tests and completed acquisitions do not erase that execution history.

A bounded read-only real-data smoke audit then checked **270 candidates**:
10 deterministic spatial samples in each of the 25 districts, plus 20 explicit
pedestrian-area samples. All checked points were inside Seoul, reproduced their
stable IDs and 20 m source chainage/grid positions, preserved source tags and
uncertainty flags, and avoided intersection with valid mapped building/water
geometries. The sample contained 257 unknown-access records, 13 unverified
mapped-permission records, and seven unsupported structure elevations. It took
1.022 seconds with 57,618,432 bytes peak RSS, one CPU and a 512 MiB address-space
cap, making no data writes or network requests. This sample does not establish
every candidate is obstruction-free or resolve missing/invalid source geometry.
See [candidate-smoke.json](candidate-smoke.json).

## Actual raster preparation and coverage

The first actual preparation run checkpointed **33 validated tiles** and retained
a **234,852,352-byte shared terrain sample index**. It was safely interrupted
after **2,322.029 seconds**, before reaching a known dense window; existing files
and interrupted artifacts were preserved. See
[preparation-interruption.json](preparation-interruption.json).

The full **109-tile** density plan selected **256×256-pixel processing windows
for 108 tiles** and **128×128-pixel windows for `x39_y111`**. These are processing
batches, not changes to the 5 m cells or 5 km output-tile extent. The dense query
would contain 111,801 samples at 256 pixels, exceeding the unchanged 100,000-point
guard; 128 pixels reduced the measured maximum to 77,718. Seoul coverage, the
10,000 m sight-support target, and both 1,000 m terrain/footprint support halos
remain unchanged.

The resumed preparation completed **109 of 109 requested tiles** in
**7,496.66 seconds**: **33 validated compatible tiles reused** and **76 newly
prepared**, with a maximum recorded worker RSS of **278,097,920 bytes**.
The final receipt is
`data/citywide/prepared/67cd2654e14de3c0/manifest.json`. Reuse validated source
hashes, receipts, grid, schema, native versions and semantic compatibility;
the old index and tile files remain recoverable.

Completed tile production does not establish complete terrain support. At
5 m cell centres, valid terrain covers **23,260,369 of 24,253,086 Seoul cells
(95.9068%)** and **23,260,369 of 84,544,177 requested support cells (27.5127%)**.
Missing terrain remains explicit NoData: **24.817925 km² inside Seoul** and
**1,532.0952 km² across the requested support**. These are raster cell-centre
measurements, separate from exact vector administrative areas. Building surfaces
retain unresolved absolute roofs wherever footprint ground cannot be supported.
The manifest retains `terrain_support_complete=false` and
`visibility_ready=false`; 5 m cell spacing does not imply 5 m accuracy.


Final offline validation of the current source package passed in **10.25 seconds**.
A separate read-only validation called the existing tile validator for all
**109 tiles** and verified **654 product checksums, grids and CRSs**, plus
**109 footprint-halo checksums**. All checks passed in **2.26971545 seconds**,
with **83,402,752 bytes peak RSS**, zero network requests and zero data writes.
These verify stored outputs; the NoData and geographic-readiness limits remain.

## Reproduction

From the repository root, on this supported Ubuntu 24.04 / Python 3.12 runtime:

```bash
python scripts/data/bootstrap.py --execute
bash scripts/data/python.sh scripts/data/citywide.py --config configs/data/seoul_citywide.json all --candidate-max-bytes 805306368
bash scripts/data/python.sh scripts/data/citywide.py --config configs/data/seoul_citywide.json validate
bash scripts/data/python.sh scripts/data/citywide.py --config configs/data/seoul_citywide.json resume
```

`validate` performs offline package integrity/schema checks. Zero-network resource
planning uses `python scripts/data/citywide.py plan`; `plan --online` permits only
bounded metadata requests. The dated source configuration and saved artifacts
are the reproducible source record. Changed upstream bytes require a separately
inspected source version; a clean future run may correctly stop on a changed or
removed object instead of silently changing its expected hash.

Raster preparation can be resumed independently with:

```bash
bash scripts/data/python.sh scripts/data/citywide.py --config configs/data/seoul_citywide.json prepare
```

The completed raster run and final full-suite outcome are recorded above. Coverage and licence blockers remain explicit.
