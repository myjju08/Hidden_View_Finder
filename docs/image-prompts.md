# Anticipated-view assets

Three original PNG illustrations generated with the built-in image generation tool on 2026-09-08 **after the default fictional Top 3 was calculated**. They are not photos, measured reconstructions or ranking evidence. The UI only reuses them when the full canonical request matches the fixed default scenario, including coordinates, eye height, time, travel mode and weather; other contexts return a prompt with `status=not_generated`.

Every card carries: “AI-generated anticipated view — actual scenery may differ.”

The retained prompts below used nominal directions of 82°, 315° and 200°. A later coordinate consistency check corrected the scenario's map-derived directions to 86.42°, 333.44° and 221.64°. The existing illustrations were **not regenerated** and are not conditioned on measured geometry or a faithful compass direction. The current API also returns a new-request prompt with the corrected direction; that prompt is not the historical prompt used to create these assets. Ranking and selected spots did not change.

## river_steps

Asset: `src/hidden_view_finder/static/images/river_steps.png`

Final generation prompt:

> Create a wide landscape 1536x1024 editorial watercolor mood illustration for Hidden View Finder's ALREADY RANKED fictional demo card '가상 물가 데크'. NOT a photograph, NOT an actual Seoul reconstruction. Scenario observer lon126.983666875 lat37.571898311 at eye height1.7m looking82 degrees east, nominal field of view60 degrees; Sep8 2026 about16:09 Asia/Seoul, fictional dry daylight scenario, no real weather observation. Only supplied fictional scene elements: near-side water edge/deck, middle water, small distant bridge shape; their exact geometry is unknown, so keep arrangement abstract and softly drawn, not architectural measurement. Muted green, warm ivory, soft blue palette; calm natural textures. Do not invent identifiable real landmarks, boats, people, reflections, a dramatic sunset, signs, or claim an unobstructed real view. Landscape only, no UI. Include a small clearly legible caption along the lower border exactly: 'AI-generated anticipated view — actual scenery may differ.' This is a scenario mood reference, never evidence for ranking.

## forest_window

Asset: `src/hidden_view_finder/static/images/forest_window.png`

Final generation prompt:

> Create a wide landscape 1536x1024 editorial watercolor mood illustration for Hidden View Finder's ALREADY RANKED fictional demo card '가상 숲 전망 쉼터'. NOT a photograph, NOT an actual Seoul reconstruction. Scenario observer lon126.970066375 lat37.576389867 eye height1.7m looking315 degrees northwest, nominal field of view60 degrees; Sep8 2026 about16:16 Asia/Seoul, fictional dry daylight scenario, no real weather observation. Only supplied fictional scene elements: greenery and forest shapes in foreground/middle-ground with soft distant mountain ridge. Exact openings and relative geometry are unknown: leave overlaps suggestive, no guaranteed unobstructed sightline. Muted forest green, warm ivory, desaturated blue palette, textured paper. Do not add towers, buildings, people, benches, signs, bridges, water, dramatic sunlight, or identifiable Seoul mountains. Landscape only, no UI. Include a small legible caption along lower border exactly: 'AI-generated anticipated view — actual scenery may differ.' Scenario mood reference only; never evidence for ranking.

## garden_frame

Asset: `src/hidden_view_finder/static/images/garden_frame.png`

Final generation prompt:

> Create a wide landscape 1536x1024 editorial watercolor mood illustration for Hidden View Finder's ALREADY RANKED fictional demo card '가상 정원 전망대'. NOT a photograph, NOT an actual Seoul reconstruction. Scenario observer lon126.972333125 lat37.566059289 eye height1.7m looking200 degrees south-southwest, nominal field of view60 degrees; Sep8 2026 about16:13 Asia/Seoul, fictional dry daylight scenario, no real weather observation. Only supplied fictional elements: low park greenery, soft garden vegetation in the middle-ground, indistinct green background. Exact garden layout and plant species are unknown, use abstract layered foliage and a modest grassy foreground. Muted olive green, warm ivory, pale daylight palette and textured paper. Do not invent flowers, structures, fountains, benches, people, towers, a city skyline, sunsets, or guaranteed openings. Landscape only, no UI. Include a small legible caption along lower border exactly: 'AI-generated anticipated view — actual scenery may differ.' Scenario mood reference only; never evidence for ranking.
