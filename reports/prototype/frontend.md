# Prototype interface inspection

The existing Korean interface in `static/index.html`, `app.js`, and `styles.css`
uses a warm paper/green visual design and a fixed SVG overview. Its previous
scenario artwork and route/arrival workflow are preserved as legacy assets.
The real-data prototype extends the same visual design through separate
`prototype.html`, `prototype.js`, and `prototype.css` files. It does not display
the fictional artwork or require routes, departure times, or arrival estimates.

The interface uses a locally bundled Leaflet 1.9.4 renderer with bounded local
GeoJSON: district boundaries/names, roads, water, and green space. It requests
features for the current viewport and never downloads public basemap tiles or
sends the full candidate inventory to the browser. Map clicks, local name
search, explicit coordinates, and optional browser geolocation set the origin.
The 1/3/5/10 km controls always represent straight-line standing-location radius.
The date/time field explicitly means intended viewing time in Asia/Seoul.

Result cards consume the same SceneEvidence object as ranking and provider
adapters. They show supported categories, bearing/FOV, sample counts including
unknowns and exclusions against the intended sample denominator, actual coordinates
and displacement, source limitations, deterministic
description method, and a computed angular-sample schematic. The schematic is
labelled “Geometry schematic — not a photograph.” Its vertical axis is labelled
in degrees and adjusts to the computed angular samples. Missing angular samples
are not drawn as invented skylines; unknown sample directions appear as dashed
lines. Target groups aggregate every sample and preserve partial/unknown support,
rather than using the first sample as evidence of whole-target visibility.
An optional validated AI comparison displays
only its enum focus and references to existing evidence IDs; it cannot change
geometry or rank. Image jobs are separate and retain the schematic when disabled,
unavailable, busy, or over budget.

A sticky experimental/incomplete-coverage notice remains visible while scrolling.
The tile overlay describes aggregate terrain validity, not per-ray or panorama
support. Provider status, source dates, unchanged global readiness, storage, and
public-deployment status are inspectable in the data dialog. The mobile layout
stacks the planner/map/cards without discarding controls. Request serials and
AbortController prevent old responses from replacing newer requests. Source
names/descriptions enter the DOM through textContent or escaped text; no
provider-authored HTML or raw SVG is inserted.

Leaflet JS/CSS passed the independent SHA256 values published in its
[official download documentation](https://leafletjs.com/download.html).
The bundle, including its BSD-2-Clause licence, is 163,753 bytes. The complete
Noto Sans KR variable font and SIL OFL 1.1 licence add 10,418,976 bytes in one
local copy. The font matches the publisher's Git blob identity from the
[Google Fonts repository](https://github.com/google/fonts/tree/main/ofl/notosanskr).
See `leaflet.json` and `font.json` for exact hashes and source metadata.

`node --check` passed for the interface and browser-check scripts. A real Firefox
155 browser run passed desktop (1440 × 1100) and mobile (390 × 844) interaction
checks, including real Gangseo cards, local map/search, evidence details, and the
no-key image fallback. The final version-aware run passed on 2026-09-11 at
15:45 UTC, returning three real partial views. It exercised the final 13 km target
inventory for a 3 km standing-location radius while retaining the 10 km ray cap.
All four sample states, mixed target groups, source-text injection, HTTP 429,
and empty responses were checked with explicitly labelled synthetic UI fixtures.
There were no page JavaScript errors or external page requests. Desktop map,
evidence-dialog and mobile-card screenshots were actually inspected: the Han
River and district names appear, Korean glyphs render, and neither viewport
has horizontal overflow. Six screenshots total 462,642 bytes.

The final suite took 23.644 seconds. Its real query reported retrieval 0.7752 s,
target retrieval 2.6415 s, geometry 4.4663 s, and end-to-end 7.9726 s while browser
rendering and accounting also ran. This is above the five-second end-to-end
goal. Combined browser profiles, retained browser/test artifacts and reports
peaked at a sampled 41,682,423 bytes under the 100,000,000-byte limit. The sampled
sum of browser-process RSS was 734,691,328 bytes; shared pages may be counted
more than once. These are sampled measurements, not guaranteed instantaneous
peaks. The detailed report is `browser-validation.json`; do not infer browser
coverage from Python or JavaScript syntax tests.

The initial Chromium launch failed on missing native libraries. After bounded
local extraction of official Ubuntu libraries, Chromium's enabled sandbox could
not start on this host ("No usable sandbox"). No sandbox was disabled. One official
Firefox distribution was then inspected and downloaded. It rendered the app
with its default security settings as the existing unprivileged `nobody` user;
the host still reports a user-namespace EPERM warning. This is evidence of a
successful browser run, not verification of every OS sandbox facility. An
`about:support` diagnostic failed separately before page inspection.

The first final-version attempt also stopped safely when the new combined
artifact scanner rejected Firefox's normal profile-lock symlink. The corrected
scanner counts this narrowly validated inert lock with `lstat`, never follows
its target, and retains output/root symlink rejection. Existing fixture tests
covered the correction before the successful browser rerun. A stale enclosing
development reservation was independently refused before another launch; the
final pass used a normal shared reservation after recorded recovery. Failed
attempt logs remain available. Playwright removed only its own transient profile;
the frontend task performed no manual cleanup of sources or prior outputs.

The browser setup first refused an inspected peak above its initial 400 MB
sub-budget, preserving its verified archive. The approved browser-only allocation
became 500 MB for Chromium, then one shared 900,000,000-byte cap for retained
Chromium, Firefox, archives and native libraries. The hard 20,000,000,000-byte
total ceiling and shared 8 GiB free-space reserve never changed. The final
dependency reuse check downloaded zero bytes and verified 880,877,568 allocated
bytes under that browser cap. Seventeen pinned official Ubuntu packages added
5,502,916 archive bytes and 29,579,485 expanded bytes. Package scripts were not
executed and system packages were not installed. Original archives remain
available for recovery. See `browser-preflight.json`, `browser-dependencies.json`,
`firefox-dependencies.json`, and the two `browser-native*.json` records.

Reproduce browser dependencies and checks without GIS acquisition:

```bash
bash scripts/prototype/python.sh scripts/prototype/fetch_leaflet.py
bash scripts/prototype/python.sh scripts/prototype/fetch_font.py
bash scripts/prototype/python.sh scripts/prototype/browser_setup.py
bash scripts/prototype/python.sh scripts/prototype/browser_native.py
bash scripts/prototype/python.sh scripts/prototype/firefox_setup.py
bash scripts/prototype/python.sh scripts/prototype/browser_native.py --firefox
bash scripts/prototype/python.sh scripts/prototype/browser_run.py --engine firefox --base http://127.0.0.1:8000 --origin 126.81774514857614,37.56567741795218
```

The runner redirects profiles/caches into accounted staging, uses unique owned
artifact directories, checks the combined artifact limit and owned-process
RSS, enforces bounded files/logs/screenshots, drops inherited
provider credentials, and uses Firefox's default security settings (or explicit
`chromiumSandbox: true` for the optional Chromium attempt). It does not install
system packages, create users, alter security settings, expose ports, or bypass a
host sandbox restriction. `--development` is only for the currently live enclosing
prototype-development reservation and is not needed for normal reproduction.
