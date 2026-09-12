# Citywide source review, 2026-09-11

This is an execution-time review of primary source catalogues and bounded HTTP
metadata requests. It does not certify source completeness, public access to
mapped places, or field visibility. Actual acquisition outcomes, file hashes,
and measured spatial coverage are recorded separately by the citywide pipeline.

| Input | Current documented distribution and decision | Date, licence, limitations |
| --- | --- | --- |
| Seoul terrain | [Seoul OA-22241 catalogue](https://data.seoul.go.kr/dataList/OA-22241/F/1/datasetView.do); existing `acquire_terrain.py` POST contract | Catalogue describes NGII 2023 topographic contour/spot SHP, `서울시 등고선.zip`, file update 2025-03-20; KOGL Type 1 attribution, commercial use and adaptation permitted. Metadata update 2026-02-11 is not observation date. |
| Building footprints/heights | [Source Cooperative conversion](https://source.coop/tge-labs/globalbuildingatlas-lod1), exact existing regional Parquet object, strict spatial HTTP ranges only | Created 2025-09-05, updated 2025-09-07. WGS84 conversion of old combined GeoJSON; retain original source labels. Public availability does not resolve downstream mixed-licence redistribution. |
| Current original GBA | [Publisher README](https://github.com/zhu-xlab/GlobalBuildingAtlas), [ODbL polygon release](https://huggingface.co/datasets/zhu-xlab/GBA.ODbLPolygon), [LoD1 release](https://huggingface.co/datasets/zhu-xlab/GBA.LoD1) | Current split separates ODbL footprints from CC BY-NC 4.0 footprints/heights. Use documented downloadable products; automated viewer/WFS extraction is prohibited by the README. |
| OSM context | [Geofabrik South Korea](https://download.geofabrik.de/asia/south-korea.html), one dated `.osm.pbf` snapshot | ODbL 1.0; © OpenStreetMap contributors. Snapshot time describes database state, not survey age or access verification. Ordinary public extract omits personal editor data. |
| Seoul boundary | Existing OSM relation **2297418** contract, preferably assembled from the same country PBF; existing single cached Nominatim request is an alternative | [OSM copyright](https://www.openstreetmap.org/copyright), [Nominatim policy](https://operations.osmfoundation.org/policies/nominatim/). Changed Nominatim bytes need a separately inspected source version, never a substituted old hash. Country extraction must preserve relation members and holes. |

The terrain script pins **45,852,601** archive bytes, **78,559,101** expanded
bytes, and SHA-256
`4fbe3c7e061b5974e7403ec116855304ed8ae321eebcc0d12c31ca8fb7be30bf`.
This is a repository-inspected fingerprint, not a separately published checksum.
Expected layers are `N3L_F001/CONT` and `N3P_F002/NUME`, metres, EPSG:5174,
CP949. Retain all sidecars and verify the downloaded schema. Existing provenance
links the [NGII national height convention](https://www.ngii.go.kr/child/content.do?sq=251)
to Incheon mean sea level; the SHP has no embedded vertical CRS. Horizontal
reprojection to EPSG:5186 is not a vertical-datum conversion.

## Live bounded probes

The following were inspected on 2026-09-11 with timeouts, without bulk payloads:

* Geofabrik `south-korea-latest.osm.pbf` HEAD redirected to
  `https://download.geofabrik.de/asia/south-korea-260910.osm.pbf`;
  Content-Length **286,988,090**, Last-Modified **2026-09-10 23:20:46 GMT**,
  ETag `"111b173a-65b2937b3c6bc"`, Accept-Ranges `bytes`.
* The [dated publisher checksum](https://download.geofabrik.de/asia/south-korea-260910.osm.pbf.md5)
  returned MD5 `6b4cb4d74d7355b0da24b9df3d48cbb6`. Compute SHA-256 locally
  as an additional fingerprint; verifying this MD5 is the independent publisher
  integrity check. Pin the dated object, because `latest` changes daily.
* A suffix request for eight bytes from
  `https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop/tge-labs/globalbuildingatlas-lod1/e125_n40_e130_n35.parquet`
  returned HTTP 206, `Content-Range: bytes 361388785-361388792/361388793`,
  eight bytes ending `PAR1`, strong ETag
  `"4f26ca9c75356a918262caf894f8d678-44"`, version
  `0i3hEPRd_oSdm7HyyIBFIWp.gt.Hg19l`, Last-Modified
  **2025-09-06 18:07:15 GMT**. This establishes metadata reachability and
  object identity, not a verified whole-object checksum or area completeness.
* The [country extraction polygon](https://download.geofabrik.de/asia/south-korea.poly)
  is published separately (5,657 bytes at this inspection). Test its geometry
  against the required source area; a country name is not a coverage proof.
* `https://map.ngii.go.kr/mn/mainPage.do` returned **HTTP 400** to a bounded
  ordinary request. No alternate direct surrounding-terrain download endpoint
  was verified. This is an access/distribution blocker, not an empty terrain area.

## Building semantics and deployment review

The [current publisher README](https://github.com/zhu-xlab/GlobalBuildingAtlas)
states that old combined LoD1 GeoJSON was split because ODbL derivative
requirements conflict with Planet-derived noncommercial data. It permits users
to combine parts for analysis while placing responsibility for licence compliance
on the user. The [publisher terms](https://tubvsig-so2sat-vm1.srv.mwn.de/terms_of_use.html)
list attribution/noncommercial conditions for GBA and ODbL conditions for OSM
and Microsoft footprints. A private noncommercial analytical subset must retain
source and height provenance. Public redistribution/deployment remains
**licence_review_required**, including the old Source Cooperative conversion.
Do not collapse the result into one blanket permissive licence.

Original GBA coordinates have an EPSG:3857 warning, including wrongly declared
original GeoJSON. The converted distribution explicitly says WGS84; validate its
GeoParquet metadata and actual numeric ranges independently. In GeoParquet 1.1,
an absent `crs` key defaults to CRS84; an explicit `null` does not establish CRS84.
Preserve source IDs, estimated AGL height in metres, `var` uncertainty when
supplied, and unresolved height as missing. Do not reinterpret AGL as absolute
roof elevation. The [GBA paper](https://essd.copernicus.org/articles/17/6647/2025/)
documents predominantly 2019 height imagery with 2018 fallback and mixed
footprint vintages; 2025 publication does not imply 2025 observation.

## Surrounding terrain investigation

The Seoul contour distribution does not promise the required 10 km surrounding
obstruction area plus interpolation halo. A compatible preferred route is
additional NGII topographic contours/spot heights with matching inspected
horizontal and vertical references, or a documented bare-earth NGII DEM.
[NGII's official brochure](https://www.ngii.go.kr/lib/file/pr_02.pdf) describes
national elevation products and the national information platform.
[The official interpretation of NGII distribution](https://www.law.go.kr/LSW/cgmExpcInfoP.do?cgmExpcDatSeq=2291887&mode=2&ofiClsCd=350102)
identifies online regional topographic downloads as free. The portal access
failure above prevented confirming a permitted, bounded unauthenticated tile
download. No account was created, new agreement accepted, or restriction bypassed.
Minimum follow-up is a permitted download of the pipeline's exact unsupported
area from NGII, including sidecars and explicit source/vertical-reference metadata,
or a documented accessible official endpoint. Storage ceilings remain unchanged.

[Copernicus DEM documentation](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM)
identifies GLO-30/GLO-90 as **surface models including vegetation and buildings**,
with EGM2008 heights, largely 2011–2015 observations. They are documented
alternatives, not compatible replacements for the Seoul bare-earth/Incheon
terrain. No DSM was downloaded, silently substituted, or resampled to imply
5 m source accuracy. Preserve unsupported terrain and quality masks.

No optional weather, crowds, imagery, model weights, basemap tiles, historic
OSM, or redundant official building/boundary products were acquired for this
review. Existing 2014 district and 2015 official-building comparison sources
have separate KOGL Type 3 constraints and are not automatically suitable for
adapted public packages.

## Reproducible local runtime fallback

The checkout's cached Ubuntu 24.04 APT metadata selected 46 native runtime
packages totalling 36,009,202 compressed bytes. One live archive URL for
`libminizip1t64` version `1:1.3.dfsg-3.1ubuntu2.1` returned 404. The
[official Ubuntu Snapshot Service](https://snapshot.ubuntu.com/) documents
timestamped distribution access. A bounded HEAD for the exact same package at
snapshot `20260824T000000Z` returned 200 and 22,204 bytes, matching the original
record. The snapshot date follows the cached official InRelease timestamps
(2026-08-23); no system APT update or version substitution was performed.

Bootstrap may use this single documented mirror only after a live object is
missing, preserving the original version, expected size, MD5, and cached
publisher SHA-256. Mirror downloads receive a separate path and source record;
authentication barriers, network restrictions, and hash failures do not trigger
mirror fallback. The cached package metadata supplies the fingerprints; this
workflow does not claim a newly downloaded signature-chain verification.
The local runtime is extracted beneath the accounted project data root without
package installation, maintainer-script execution, or administrator changes.

## Bounded preparation after source acquisition

The inspected contract requires 109 fixed 5 km tiles at 5 m cell spacing; 43
tile rectangles intersect Seoul. Each tile uses a 1 km footprint terrain collar.
The full rectangles, including unsupported buffer cells, remain in the product.
The existing TIN starts with 256-pixel windows, a 1 km interpolation halo and a
500 m maximum triangle edge. A bounded read-only count of the exact local-point
queries now selects the largest safe window, halving down to 16 pixels if needed.
The native sampled-contour local linear TIN and obstruction algorithms are
unchanged. Cell spacing, source sampling, halos and triangle-edge policy stay
fixed; the smaller window changes which local points enter each TIN.
Masking and obstruction rasterization use bounded
512-pixel windows; this changes processing batches, not cell spacing or coverage.
An entire footprint-halo rectangle disjoint from the Seoul source domain receives
explicit NoData without running interpolation. A halo touching Seoul still uses
the existing builder. Missing terrain leaves whole-footprint roofs unresolved.

The [read-only source inspection](../reports/citywide/preparation-inspection.json)
found 2,076,954 contour/spot samples before deduplication, below the three-million
sample cap. The [exact conflict preflight](../reports/citywide/terrain-conflict-preflight.json)
found 2,069,439 unique coordinates and no duplicate elevation disagreements over
the existing 0.01 m tolerance. The actual shared SQLite index is 234,852,352 bytes.
Its 20 held-out spots gave a 2.3752 m diagnostic RMSE; this small selected subset
does not establish population accuracy or field validation.

The first preparation run published 33 validated tiles before a deliberate
checkpoint for the local-density preflight. One pending tile, `x39_y111`, had
111,801 local samples in its densest 256-pixel window, exceeding the unchanged
100,000-point cap. A read-only evaluation of all 109 tile grids selects 128 pixels
for that tile and 256 for the other 108. Its maximum at 128 pixels is 77,718.
Changing a TIN window can change local point subsets, so this is recorded per
tile and does not claim numerical identity with an unbuilt 256-pixel result.

The new coordinator accepts only one explicitly pinned predecessor plan and
coordinator hash for reuse. Source hashes, native builder hashes, dependency
versions, grids, sampling, both halos, base-height policy and all other recipe
settings must agree. Validated completed tiles are reused only when their fresh
window selection is still 256; the shared sample index is independent of that
window choice. New bundles use same-filesystem hardlinks and explicit migration
receipts, retain original manifests, and charge temporary aliases against the
staging ceiling. Any incompatible or corrupt predecessor is preserved and
refused. The completed run retained 33 validated predecessor tiles and built 76 new ones;
all 109 final tiles passed file/hash validation. Source support remains incomplete.

The tile footprint workspace adds 1 km around each fixed tile, and local TIN
queries add another 1 km around that workspace. The union of all hypothetical
query rectangles is 3,221 km², including 934.4915 km² outside the declared 11 km
terrain-acquisition halo. The source-domain shortcut leaves only 47 actual TIN
workspaces: their query union is 1,511 km², entirely inside the requested 10 km
support, but 904.6741 km² lies outside the official Seoul source domain. Plans
record the exact WGS84 union geometries and projected areas separately from the
requested acquisition geometries. These are processing extents, not supported
elevation coverage; unavailable source cells remain NoData.

Before compression, full 256-by-256 storage-block padding requires
2,914,516,992 bytes for the retained rasters: 12 bytes per final tile cell and
6 bytes per retained halo cell. Add the 784,777,216-byte sample-index cap,
230,686,720 bytes of capped worker logs, and 32 MiB of metadata. The resulting
3,963,535,360-byte retention estimate becomes **4,954,419,200 bytes** with a
25% uncertainty margin. Including an additional active tile writer gives a
conservative **5,686,849,536-byte incremental peak estimate**. These figures
assume no compression savings; existing files and failed attempts remain
additional shared usage. The live plan records this derivation. Every stage
still requires its own locked reservation under the exact 20 GB total ceiling,
8 GiB free-space reserve and 4 GiB aggregate temporary ceiling.

Building source IDs are not unique feature keys. A bounded probe found 1,948
distinct footprints sharing `3dglobfp/0` among the first 20,000 local FIDs.
The complete receipt reports seven invalid geometries retained without
acquisition repair, 50,327 nonunique source/ID groups, 71 negative supplied
variance values, and 5,255 positive heights below one metre. These
source values remain preserved and flagged. Preparation retains unique source
GeoPackage FIDs, deduplicates equal geometry/height/base records, and does not
use invalid variance values in roof calculations. Heights remain model
estimates, not surveyed elevations. Any repairs used in a derived surface are
reported separately; they do not change the retained invalid source geometry.

The normalized input artifacts are published, but `normalized_ready=false`
remains gated by those seven invalid source building geometries, unresolved OSM
geometry issues and 36 incomplete relevant relations of unproven spatial scope.
Country extraction coverage, selected row-group completion and usable raster
tiles do not establish complete real-world or relation coverage.
`visibility_ready=false` remains separate from acquisition completion and public
deployment licence review. No product is labelled field-verified.

Run or resume the bounded preparation with:

```sh
bash scripts/data/python.sh scripts/data/citywide.py --config configs/data/seoul_citywide.json prepare
```

## Measured raster coverage and overlap limitations

The completed `67cd2654e14de3c0` product has supported terrain at 23,260,369
of 24,253,086 Seoul cell centres (95.9068%) and 23,260,369 of 84,544,177
requested support cell centres (27.5127%). Missing cells remain NoData; neither
full Seoul terrain support nor full surrounding support passed. The dense tile
`x39_y111` completed 121 TIN windows at 128 pixels, while the other 108 tiles
kept 256-pixel windows. The maximum measured worker RSS was 278,097,920 bytes.

The [all-tile overlap audit](../reports/citywide/raster-overlap-quality.md)
compared matching coordinates in cropped DTMs and neighboring retained halos.
The broad overlap maximum was 2.7707 m, with 32 validity disagreements at
16 unique coordinates. Within one cell of actual shared final-tile edges the
maximum was 0.0106125 m; within 20 m it was 0.1324844 m, with no observed
validity disagreement in either band. These narrower bands remain separate
from broad halo comparisons. The report preserves every nonzero comparison
and NoData anomaly; it does not certify seams, terrain accuracy or visibility.
