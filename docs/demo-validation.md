# Demo validation — 2026-09-08

The demo is implemented and executed locally. The scenario is fictional; actual Seoul inputs support an approximate point-visibility and pedestrian-candidate search. It is not a production travel recommendation service.

## Automated checks

- GIS environment: **263 tests passed** across the existing visibility engine and new recommendation/API regression suite.
- Without GDAL: **89 passed, 1 skipped** for the recommendation/API suite. The optional native-adapter test requires the GIS environment; downloaded datasets are not needed for the remaining cases.
- Chromium 145 / Playwright 1.58.0: **13 browser checks passed**, no page or console errors. Desktop 1440×1000 and mobile 390×844, three decoded/labeled images, comparison table, evidence panel, card details, JSON download, constraints, reset, changed-request image invalidation, and actual Seoul candidate separation.
- Package wheel includes both Python packages, the HTML/CSS/JS and three illustration assets; no source dataset is packaged.
- A fresh staged checkout with **no `data/` directory and no GDAL** started through `python scripts/demo/run.py`; HTTP requests returned three fictional recommendations and loaded all three PNGs. Requesting Seoul mode returned the explicit missing-data response (409).

[Browser result JSON](../reports/demo/browser-checks.json) · [Service timing JSON](../reports/demo/validation.json)

Regression coverage includes unknown-weight renormalization, stale observations including misleading validity intervals, incomplete weather variables, opening hours throughout the stay, accessibility, population versus visitor crowds, duplicate experiences, source-cell snapping, barriers and one-way route sinks, provider failures, per-arrival forecasts and HTTP request/path validation.

## Results actually produced

Default fictional request: 2026-09-08 16:00–19:30 Asia/Seoul; nature/river, quiet, walking ≤45 min and ≤2,500 m; K=3.

| Scenario spot | Score | Evidence coverage | Image |
| --- | ---: | ---: | --- |
| Fictional water deck | 92.13 | 100% | Generated mood illustration |
| Fictional forest viewpoint | 74.68 | 100% | Generated mood illustration |
| Fictional garden viewpoint | 70.70 | 90% | Generated mood illustration; crowds unknown |

These percentages describe the original criterion weights with evidence, not confidence probabilities. All scene, route and access evidence in this table is fictional. The scenario also returns one unverified and three excluded candidates; duplicate and eligible non-Top-K spots do not pad the final three.

Actual Seoul request starts at the mapped Myeongdong Station exit 3 and searches a 1.8 km / 5 m window around one approximate N Seoul Tower upper point. One native viewshed precedes pedestrian-node sampling and shortest mapped walking paths.

- 6,295 pedestrian nodes inside the radius: **3,174 visible, 1,543 blocked, 1,578 excluded, 0 unknown** at their containing raster cells.
- Deterministic 100 m spatial bins yield a bounded sample of 120 candidates.
- **0 confirmed, 15 unverified, 85 excluded, 20 duplicates.** Current full-route accessibility and visiting hours remain unverified; no actual Top 3 is fabricated.
- Map coordinates and effective observer cell coordinates are distinct. The modeled tower point is approximately 489.07 m in the terrain vertical reference, formed from DTM + 236.7 m. Its surveyed foundation datum is unresolved; it is not an independently measured apex.

Open-Meteo was called separately at **2026-09-08T09:56:43.952962Z** and returned a forecast hour of **2026-09-08 21:00 Asia/Seoul**: precipitation 0 mm, cloud cover 15%, visibility 20,000 m and wind 1.36 m/s. This verifies provider integration only. It is a model forecast, not a site observation; its model-run time was not supplied. Service timings below disable network weather.

## Measured service times

Environment: Linux-6.8.0-138-generic-x86_64-with-glibc2.39; Python 3.12.14; 32 logical CPUs; GDAL 3.8.4, NumPy 2.5.2. Container storage type is unknown. This is a shared development machine, not an isolated benchmark host.

| Measurement | Actual time | Interpretation |
| --- | ---: | --- |
| Service-module import | 46.76 ms | Does not include interpreter process startup |
| Service initialization | 0.11 ms | Lazy; does not open Seoul rasters yet |
| Scenario warm, 30 requests | median 2.65 ms / p95 5.91 ms | Ranking + presentation, no recommendation-result cache |
| Seoul first call in this process | 3.768 s | Includes context/graph/engine opening; native cache status `miss` |
| Seoul repeated identical call | 1.310 s | Native viewshed `memory_hit`; routing/ranking/presentation still run |
| Native dense portion of first Seoul call | 33.50 ms | Only GDAL viewshed at 1.8 km; not full service latency |

Peak resident memory in the timing process: **346.1 MiB**. Three generated PNGs total **9,070,551 bytes**. Browser/HTTP transfer, image decoding, export and external weather are outside these in-process service timings. Filesystem caches were not flushed. The two Seoul runs are integration smoke measurements and cannot provide a useful service median/p95. Thirty scenario runs vary only the travel-time limit by index/100 minute; they are not spatially distributed real-landmark benchmarks.

The existing [engine benchmark](benchmarks.md) separately measures real 1/3/5 km and synthetic 10 km coverage, warm uncached native queries and exact cache hits. Its 5 km timing must not be substituted for the recommendation service timings above.

## Reproduce and remaining requirements

```bash
.venv/bin/python -m pytest -q
python3 -m pytest tests/test_recommendation.py tests/test_demo_service.py -q
.venv/bin/python scripts/demo/validate.py --seoul
# With the demo server running and optional Playwright prepared:
.venv/bin/python scripts/demo/check_browser.py
```

See [demo setup](demo.md) for test dependencies and data-free execution. Exact performance varies by load and environment. The scripts write only tool-owned outputs under `data/demo/` by default. Reviewed aggregate reports and UI screenshots are published; source terrain, buildings, map features and prepared rasters remain local and ignored by Git.

Before actual confirmed recommendations are possible, connect current entry/opening information, validated complete walking accessibility, applicable transport schedules where requested, and more landmarks with verified heights and vertical references. Crowd observations, trees/walls/construction and broad river/forest/skyline composition remain absent or unknown. Runtime image generation is not connected: only the exact default fictional request reuses the three included mood illustrations; other requests return prompts marked `not_generated`. [Historical image prompts and direction correction](image-prompts.md) explain their provenance.
