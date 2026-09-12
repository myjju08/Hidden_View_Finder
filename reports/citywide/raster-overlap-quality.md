All **109 completed tiles** were audited at identical raster coordinates. This is an internal consistency check; it does not certify seams, elevation accuracy or visibility.

| Comparison area | Same-coordinate comparisons | Jointly valid | Maximum absolute difference | NoData disagreements |
| --- | ---: | ---: | ---: | ---: |
| Broad overlap with adjacent retained halos | 92,320,000 | 22,777,427 | 2.7706871 m | 32 |
| Within one cell of shared final edges | 388,000 | 96,211 | 0.0106125 m | 0 |
| Within 20 m of shared final edges | 1,552,000 | 384,806 | 0.1324844 m | 0 |
| Within 5 m of corner-only contacts | 368 | 96 | 0 m | 0 |
| Within 20 m of corner-only contacts | 4,784 | 1,248 | 0 m | 0 |

The broad audit checked 194 edge-adjacent pairs and 184 corner-only pairs in both
directions: **756 exact grid-alignment checks** and **436 raster fingerprints**.
GDAL NoData masks and finite-value checks defined support; cropped terrain-quality
masks agreed with those validity checks. The median/P90/P95/P99 difference is
exactly zero in every table row. Broad quantiles follow from the exact zero-count
bound; the independent reservoir estimate also returned zero. Edge-band quantiles
were calculated from all jointly valid band comparisons.

Broad overlaps had 380 nonzero comparisons: 154 exceeded 0.01 m, 89 exceeded
0.1 m and 13 exceeded 1 m. None exceeded 5 m. The maximum compares 20.0 m in
cropped `x38_y109` with 22.7706871 m in the retained halo of `x38_y108` at
EPSG:5186 `(192562.5, 545357.5)`, **357.5 m from their shared final edge**.
The >1 m edge cases lie 292.5–722.5 m from their shared edge. The 32 NoData
disagreements concern **16 unique coordinates**, 477.5–997.5 m from shared
edges (corner-only distances are reported separately).

In the one-cell edge band, 62 comparisons differ and one exceeds 0.01 m.
Within 20 m, 100 differ, eight exceed 0.01 m and one exceeds 0.1 m.
There are no >1 m or NoData disagreements involving the 128-pixel tile
`x39_y111`. These observations preserve the existing builder's local-window
differences; no postprocessing or numerical identity is claimed.

One-cell means centres 2.5 m from an actual shared edge on the 5 m grid;
20 m includes four rows/columns on each side. Every subtraction compares the
same coordinate in a cropped DTM and its neighbor's retained halo.
Different-coordinate neighboring cells were never subtracted. Corner-only pairs
use Euclidean distance to the shared point and are not shared-edge seam tests.
Overlapping pairs/directions repeat some coordinates, so counts are not unique
geographic coverage counts. Both-NoData agreement is missing support, not valid
terrain. No acceptance tolerance or seam certification is inferred.

[Machine-readable results](raster-overlap-quality.json) preserve counts, exact
edge quantiles, resource measurements, directions and methods.
[All 412 anomaly records](raster-overlap-anomalies.json) retain every nonzero
comparison, including floating-point-scale differences, and all NoData cases.
The original rasters and source/native recipes are unchanged.

Reproduce the bounded read-only audits in the prepared repository runtime:

```sh
bash scripts/data/python.sh reports/citywide/audit_raster_overlap.py
bash scripts/data/python.sh reports/citywide/audit_raster_edges.py
```

Both commands print JSON to stdout. They use a 512 MiB address-space limit,
a 16 MiB GDAL cache, one CPU and windows no larger than 512×256 pixels.
The original full audit used 18.69 seconds and 118,157,312 bytes peak RSS;
the edge/all-anomaly audit used 12.68 seconds and 105,115,648 bytes.
Saved-entrypoint verification results are recorded in the JSON report.
