# Citywide acquisition safety validation

Audit date: 2026-09-11. Acquisition and normalization produced a checksummed source package, and preparation completed all **109 requested 5 km tiles** at 5 m cell spacing: **33 reused after compatibility checks and 76 newly prepared**, in 7,496.66 seconds. Geographic coverage, access, model accuracy and field verification remain separate from successful artifact validation. The surrounding compatible terrain is unavailable, and quality masks preserve interior and buffer gaps; `visibility_ready` remains false.

The final full repository suite, `full-tests-08`, reported **518 passed**, with zero failures, errors or skips. The retained JUnit receipt was independently inspected. Scoped checks included 60 safety tests, 32 state-checkpoint tests and 37 migration tests; preparation/window fixtures and earlier source tests are included in the full suite. Failed intermediate fixture runs were corrected before the passing results and are not counted as successful runs. Tests simulate pressure and interruption; they never filled the disk or downloaded citywide inputs. Commands and native/real-data validation are recorded in [validation.md](validation.md).

The scoped migration command actually executed was:

```sh
bash scripts/data/python.sh -m pytest tests/test_citywide_prepare_reuse.py -q --basetemp=data/citywide/staging/preparation-reuse-tests-20260911-03 -p no:cacheprovider
```

That run reported 37 passed and one GDAL exception-policy future warning. The state-checkpoint command used `tests/test_citywide_pipeline.py` and the fresh accounted `--basetemp=data/citywide/staging/state-recovery-tests-20260911`, reporting 32 passed. Use a new task-owned basetemp for future tests: pytest can replace a directory supplied through that option. No tests, GIS mutations or cleanup were performed during this final report audit.

The mandatory policy in [acquisition_safety.py](../../src/seoul_visibility/acquisition_safety.py) remains:

| Constraint | Exact bytes |
| --- | ---: |
| Maximum total accounted footprint | 20,000,000,000|
| Minimum free space on receiving filesystems | 8,589,934,592|
| Maximum aggregate temporary footprint, included in total | 4,294,967,296|
| Protected checkpoint/report room | 16,777,216|
| Maximum individual JSON checkpoint | 8,388,608|
| Selected geographic output size target | 8,589,934,592|

Configuration cannot raise the 20 GB ceiling; the former 20 GiB default is rejected. Reservations include at least 25% margin on uncertain incremental peaks. Existing data, raw sources, dependencies, caches, partials, journals, staging, metadata and repository files are counted. Total accounting deduplicates filesystem device/inode and charges the larger of logical and filesystem-reported allocated bytes. A hardlink alias in staging still counts against the temporary limit. Category sums include logical aliases and must not be presented as physical usage.

A shared writer lock serializes stages and checkpoint publication. Native children verify an explicitly inherited locked descriptor, live parent and matching reservation. Streamed downloads retain strict 206/Content-Range and identity checks; full partials require renewed upstream identity and validation before publication. Downloads and ZIP members use bounded Linux KEEP_SIZE allocation. ZIP paths and actual expansion are checked. Small-write credits, native output/page limits, memory limits, tile/batch sizes and monitoring margins bound work. None of these application safeguards constitutes an OS disk quota: no privileged quota or mount was created. Unrelated writers can consume shared free space between observations.

State recovery writes a durable ownership receipt with expected size/hash before each unique checkpoint writer. Complete matching owned writers can recover interrupted publication; old partials stay in place with recorded hashes. Legacy unowned writers are never promoted or overwritten. Corrupt canonical state, stale invocations and absent canonical state without a complete owned matching writer stop with `checkpoint_blocked`. Read-only status never takes ownership of another live writer's files. Atomic directory publication protects completed packages and terrain-index bundles. Interrupted terrain and surface outputs require matching ownership and are preserved before rebuilding.

Measured selected output sizes after preparation are:

| Existing selected output | Logical file bytes |
| --- | ---: |
| Source package `seoul-20260911-v1-6abf3cc88682` | 866,156,702|
| Complete prepared directory `67cd2654e14de3c0` | 468,384,453|
| Both complete directories together | 1,334,541,155|

The combined selection accounts for **1,339,324,735 bytes including directories**, below the 8,589,934,592-byte target. This is the actual retained selection, including metadata, logs, recovery inputs and the development sample index; it is not a hypothetical stripped deployment bundle. The 654 final tile rasters occupy 136,749,422 logical bytes. The 109 retained footprint-ground halos occupy 91,853,208 logical bytes. The 234,852,352-byte sample index supports rebuilding rather than reading finished raster values; it remains intact and shares its inode with the previous recipe. Reused raster values share inodes with their validated originals. Old manifests remain intact; historical cleanup is not relabelled as a new deletion. The prepared manifest's `retained_bytes=468170323` was computed before writing its own 214,130-byte file; the complete directory inventory above includes it.

At **2026-09-11 T 13:26:36.047975+00:00**, immediately before publishing these reports, the shared-budget snapshot measured **2,585,895,159 accounted bytes**, **422,557,440 temporary bytes**, and **24,372,367,360 available filesystem bytes**. Accounted bytes include the 5,242,880-byte conservative external-fixture allowance. These are timestamped observations, not a promise that unrelated filesystem usage cannot change. Startup free space was 26,043,863,040 bytes with 21,258,240 allocated project bytes and no project GIS inputs, leaving at most 17,453,928,448 additional bytes after the 8 GiB reserve before other constraints and margins.

[storage-history.json](storage-history.json) preserves distinct historical observations. An earlier package-command tool report observed 1,212,068,880 temporary bytes and 2,031,789,633 accounted bytes. A later package report captured a sampled temporary maximum of **1,288,976,098 bytes**. The first preparation invocation recorded sampled maxima of 2,478,379,741 accounted bytes and 792,252,317 temporary bytes. The successful resumed preparation recorded **2,593,484,574 accounted bytes** and 657,655,655 temporary bytes. These invocation counters restart; their largest recorded values are not independently verified continuous lifetime peaks. No recorded sample crossed the mandatory ceilings.

Dependency storage is part of the same budget:370,636,235 logical file bytes, or 384,297,151 max-per-inode accounted bytes including directory overhead in the inspected dependency tree. Downloaded dependency archives, the native runtime and Python packages remain retained. The reported network total is a **480,493,439-byte lower bound**. Earlier interrupted/uncommitted building reads and some metadata probes were not completely metered. The building receipt's latest-invocation 61,713,472 bytes and its committed-row-group sum 62,368,044 bytes have different scopes and must not be added blindly. Local subset output hashes and committed range validation do not imply verification of a whole remote-object checksum.

Known preserved interrupted outputs remain charged:22,134,784 bytes from the failed OSM normalization,84,287,488 bytes from the first candidate attempt, and 3,605,196 bytes from the interrupted raw terrain raster. Their total is 110,027,468 logical bytes and 110,030,848 allocated bytes. They are interrupted output sizes, not failed network-transfer totals. Original raw sources and the validated sample index remain available.

The first candidate cap was 256 MiB. A source-based upper count 648,667 and early measured average 818 bytes per point projected 530,609,606 bytes, or 663,262,008 with 25% margin. The coordinator interrupted the first attempt after 297.012 seconds while it held 84,287,488 bytes, then used a persisted 768 MiB artifact cap inside the unchanged shared ceilings. The completed output contains 582,255 candidates in 443,691,008 bytes, SHA-256 `8f23a5a6e6cb60071b894bf8452d8016d460ce671bb8a23f085d3f4a14b2b5c6`, with zero invalid geometries or duplicate source IDs. It does not certify access, current opening or scenic quality.

The first preparation kept 33 validated tiles before a controlled interruption. A bounded probe found five 256-pixel interpolation windows in future tile `x39_y111` above the unchanged 100,000-point cap, with 111,801 points at the maximum. The 128-pixel probe maximum was 77,718. The new recipe uses 108 tiles with 256-pixel windows and one with 128-pixel windows, preserving 5 m cells, all source/sampling/height settings, the 10,000 m sight-support radius and declared halos. Tiles with changed interpolation windows were recomputed; reused tiles passed exact source/native/dependency/grid/settings compatibility and content checks. Smaller local TIN windows can change point subsets, so numerical identity is not claimed for recomputed tiles.

Cleanup was reconciled from **218 unique preparation-journal inodes**:763 completed deletion events totaling 952,967,829 logical bytes, with no unfinished ledger entries. The old 33 tiles account for 231 events/217,502,678 bytes; the new 76 tiles account for 532 events/735,465,151 bytes. These remove only recorded task intermediates after validated successors. A rename or deletion intent is not counted as completed cleanup. Physical disk bytes freed were not measured and remain unknown; neither logical deletion totals nor changes in shared free space establish that value.

Two historical limitations and one cleanup-order deviation remain explicit:

- Five early safety-test invocations used pytest's default scratch location before accounted basetemp paths were adopted. Exact retained bytes were not measured. The unchanged total ceiling continues to include a 5,242,880-byte conservative allowance; unrelated private temporary directories were not searched. Later fixtures stayed under the accounted staging tree.
- Earlier network metering is incomplete as described above. The recorded transfer figure is a lower bound rather than an exact lifetime total.
- Early dependency recovery deleted one `proj.db.part`, **8,388,608 logical bytes**, before a validated extracted successor existed. Allocated bytes were not recorded. Its durable ledger is `data/citywide/dependencies/site/pyproj/proj_dir/share/proj/proj.db.part.owner.json.cleanup.json`. The verified original wheel remained intact, SHA-256 `1edc34266c0c23ced85f95a1ee8b47c9035eae6aca5b6b340327250e8e281630`, so recovery remained possible; retaining that archive did **not** satisfy the requested successor-before-cleanup ordering. This historical action is not compliant with the final cleanup policy. Including it separately yields 764 recorded artifact deletions and 961,356,437 logical bytes; it must not be folded into the compliant preparation total without this qualification.

The final policy preserves interrupted ZIP/normalized/raster outputs and refuses generic cleanup without a distinct validated successor receipt and matching hash. Source age, missing relation geometry, unrepaired building geometries, licence review, terrain gaps and field verification remain recorded separately in the source and coverage reports. All 109 tile artifacts being prepared does not make unsupported terrain valid or make the visibility product ready.
