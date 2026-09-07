# Measured synthetic point-visibility benchmark

The 5 km / 5 m query met the initial design goals on this development machine:
**0.422 s median and 0.467 s p95 for warm uncached computation**. An identical
result-cache hit took **0.529 ms median**. These are synthetic performance
measurements from the initial synthetic-only run. Real Seoul inputs were acquired
later; their separate measurements are in [the real-data report](../docs/benchmarks.md).

The final run used GDAL 3.8.4 `GVM_Edge/GVOT_NORMAL`, Python 3.12.14, NumPy 2.5.2,
and pyproj 3.7.2 on Linux with an AMD Ryzen Threadripper PRO 3955WX (16 physical,
32 logical CPUs). Available RAM was about 231 GiB. The filesystem reports
`overlay`; physical storage type is unknown. Full machine and cgroup details,
package versions, target coordinates, and raw timings are in
[final/benchmark.json](final/benchmark.json) and [final/runs.csv](final/runs.csv).

The fixture is 4,801 × 4,801 pixels at 5 m, covering a 24,005 m square in
EPSG:5186. Its fictional smooth terrain and synthetic roof columns occupy
6,151,288 compressed bytes; unusually simple, compressible terrain does not
represent real Seoul I/O or accuracy. Five target locations were distributed
around the central area using seed 1729. Target height was explicitly 120 m AGL,
observer eye height 1.7 m, and curvature coefficient 6/7.

Each radius has **30 timed uncached queries** cycling through the five targets,
plus 30 exact cache hits. Target locations were prewarmed. The filesystem cache
was uncontrolled and never flushed; these are not cold-disk measurements. Large
windows can exceed the 64 MiB GDAL block cache. All output arrays were streamed
through the benchmark rather than retained for every query.

| Radius | Output dimensions | Uncached median | Uncached p95 | Cache-hit median |
|---|---:|---:|---:|---:|
| 1 km | 401 × 401 | 0.0154 s | 0.0232 s | 0.535 ms |
| 3 km | 1,201 × 1,201 | 0.1567 s | 0.1786 s | 0.528 ms |
| 5 km | 2,001 × 2,001 | 0.4217 s | 0.4672 s | 0.529 ms |
| 10 km | 4,001 × 4,001 | 2.4190 s | 2.6491 s | 0.536 ms |

At 5 km, median stage times were 0.242 s for window reads and strict surface/
coverage validation, 0.105 s for native dense visibility, and 0.069 s for masking
and result assembly. Independent stage medians need not sum to the total median.
The measured bottleneck was the read/validation stage. A controlled cache-size
experiment used another 30 uncached queries per size and checked identical
output hashes for all targets:

| GDAL block-cache cap | 5 km median | Read/validation median |
|---|---:|---:|
| 64 MiB | 0.405 s | 0.232 s |
| 128 MiB | 0.417 s | 0.246 s |
| 256 MiB | 0.396 s | 0.227 s |

The roughly 2% difference at 256 MiB did not justify quadrupling the cache cap;
64 MiB was retained. See [block_cache_profile.json](block_cache_profile.json).
The original full baseline remains in [full/benchmark.json](full/benchmark.json).

One-time fixture generation took 6.435 s. Inspection of prepared raster metadata
and blockwise missingness took 1.775 s; real-source preprocessing was not
benchmarked because real inputs were unavailable. A separate small synthetic
contour/spot/SHP pipeline ran inspect, plan, prepare, query and resume successfully:
preparation core took 1.388 s for a 300 × 300 grid, and the 500 m query core took
8.90 ms. All raw SHA-256 digests stayed unchanged. The fictional planar terrain
had zero RMSE on 45 held-out spots; that is an analytical fixture check, not
Seoul elevation accuracy. See [preprocessing_fixture.json](preprocessing_fixture.json). Fresh process launch
plus package import took 0.679 s, including 0.447 s for the import. Initial
prepared-data opening took 0.061 s. Neither startup measurement claims cold disk.

The 16-observer independent Python LOS batch took 0.267 s at 5 km and 1.232 s at
10 km. No JIT or compiled sparse kernel is used. Optional 5 km GeoTIFF export
took 0.041 s and produced 36,603 bytes; 10 km export took 0.112 s and produced
109,016 bytes. Exports were measured separately and then removed. The largest
result array was 16,008,001 bytes. Maximum sampled RSS was 659 MiB; process
high-water RSS was 657 MiB. The RAM result cache is bounded at 128 MiB; the disk
result cache uses zero bytes.

Native/reference validation is recorded separately in
[backend_validation.json](backend_validation.json): 124 false-visible and 14
false-blocked disagreements across 5,693 sampled open-ground cells, relative to
the specified closed-column reference convention. The simple flat and continuous
wall regressions agree. Roof-source, corner-contact, near-grazing, and propagated
horizon cases can disagree because GDAL's raster horizon is a different surface
intersection convention. These counts are diagnostic model disagreements, not
real-world error rates or a demonstrated visibility bound.

No local 2 m benchmark product exists; none was made by upsampling 5 m data.
The subsequent central-Seoul validation uses documented terrain, explicit height
fields and a Seoul output boundary; see [the data sources](../docs/data-sources.md)
for its coverage and estimated-height limitations. Public access, vegetation,
overhangs and visual attractiveness remain outside this model.

Reproduce with fresh output paths:

```bash
seoul-visibility synthetic --output data/benchmark --size-m 24000 --seed 1729
seoul-visibility benchmark data/benchmark/manifest.json --output reports/reproduced --runs 30
seoul-visibility benchmark data/benchmark/manifest.json --output reports/cache-reproduced.json --runs 30 --profile-block-cache
```

The synthetic command reuses a matching complete fixture. Benchmark reports and
exports do not overwrite existing files.
