# Optional prototype providers

The no-key application uses geographic evidence, deterministic ranking and geometry schematics. These do not require OpenAI. The optional adapters in `src/hidden_view_finder/prototype/ai.py` use the actual Responses and Image generation HTTP APIs. This task did not make paid calls: `OPENAI_API_KEY` and AI enablement were absent. Mocked adapter calls are tests, not proof of account/model availability.

The implementation was checked against official [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [Image generation](https://developers.openai.com/api/docs/guides/image-generation), and [pricing](https://developers.openai.com/api/docs/pricing) documentation on 2026-09-11. Model names and prices are deliberately unset because availability and the selected model's exact price require review by the operator.

## Enable authorized calls

1. Choose a text model that supports strict Responses JSON Schema output and an image model that supports the Image API options below. Confirm access in your own API project.
2. Review current official pricing for those exact model IDs. Record it under `ai.pricing` in `configs/prototype.json`; leave paid calls disabled during setup. Pricing records expire after 30 days. A model override invalidates a price record for another model.
3. Set an explicitly authorized, positive `ai.daily_usd`, along with `ai.daily_calls` (1–100; default 20) and `ai.daily_images` (0–daily_calls; default 5). These limits include unsuccessful/uncertain calls. The default is zero USD.
4. Set the server environment using the names in `.env.example`. Start the server with both `ai.enabled: true` and `HVF_AI_ENABLED=true`. A key alone authorizes nothing. The `.env` file is ignored by Git and must not be placed in the static directory.

Required text pricing fields are `verified: true`, `verified_at` (timezone-aware ISO timestamp), `source_url` (an official `developers.openai.com` or `platform.openai.com` HTTPS page), `model`, `input_per_million_usd`, and `output_per_million_usd`. Values must be positive finite USD amounts. Do not copy the synthetic prices from tests into operational configuration.

Required image pricing fields are the same provenance fields plus `size: "1024x1024"`, `quality: "low"`, `maximum_request_usd`, `includes_prompt_cost: true`, and `maximum_prompt_bytes: 12000`. The operator must verify a conservative **maximum**, including prompt cost, for this fixed one-image request. There is no assumed per-image price. If a reliable bound cannot be established for the chosen model, keep image calls disabled. The generation request uses `n=1`, JPEG output, and compression 75; no arbitrary model/size/quality is accepted from a client.

The exact restart command after changing server configuration is:

```bash
bash scripts/prototype/python.sh scripts/prototype/run.py serve --port 8000
```

The server inherits environment variables; it does not automatically execute `.env` files. To stop new paid calls across running instances immediately, create `data/prototype/ai/STOP_PAID_CALLS`, or restart with `HVF_AI_KILL_SWITCH=true`. Existing in-flight calls cannot be unbilled. The file's absence does not override other authorization checks.

## Evidence contract

One optional text call compares at most three supported views. It receives only allowlisted category enums, view/evidence IDs, relative bearings/distances, composition class and the complete visible/blocked/unknown/excluded denominator. Exact user origins, free text, source names, paths and unrestricted instructions are excluded. Strict output selects existing evidence IDs and a presentation-focus enum. Local validation rejects invented IDs, unknown samples, extra properties, missing views and rank changes. The original deterministic order remains authoritative; no generated free prose becomes a scene claim.

The image brief uses this same supported evidence, orientation, five-degree solar classes, Seoul calendar season and explicitly qualified weather class. A forecast class is used only inside its valid time range and with a retrieval no older than six hours. Hypothetical weather is labelled hypothetical and gets a distinct cache key. Missing/stale weather stays unknown. Roof samples do not establish facades; mapped wooded terrain does not establish canopy; incomplete sectors stay indistinct. No image influences ranking or geometric validation.

Every generated image is labelled **“AI-generated atmosphere preview. Actual scenery may differ.”** The fallback remains the geometry schematic.

## Spending, concurrency and recovery

`spending.sqlite` uses SQLite transactions under the same cross-process writer reservation as acquisition/runtime storage. It stores daily aggregate maximum reservations, not keys, prompts, origins or provider output. At most 100 calls/day and 366 daily rows are retained; SQLite is capped at 2 MiB with a bounded rollback journal. Reservation occurs before each billable POST. Text reservations use the full bounded serialized input byte count plus 1,024 protocol tokens as a conservative token allowance, and configured maximum output tokens (128–1,000; default 512). Images reserve the reviewed maximum request price. No refunds are made automatically, even on refusal, timeout, schema failure or crash. This can under-use the authorized budget; it prevents ambiguous failures from silently creating more spending authority.

This is an application limit, not an OpenAI billing guarantee or provider-side quota. Incorrect/outdated configured prices cannot guarantee the provider's bill. Configure provider-side project limits too where available. There are **zero automatic billable retries**. Authentication, rate-limit, body-size, redirect, schema, timeout and storage failures have separate public codes without raw response bodies or secrets.

The transport accepts only the fixed official `/v1/responses` and `/v1/images/generations` endpoints, rejects redirects, disables unrelated netrc credential discovery while honoring configured network proxies, bounds response bytes with or without Content-Length, and checks format. An owned spawned process enforces a total wall deadline of 15 seconds for text and 60 seconds for images; it is terminated/joined on timeout. The child has a 512 MiB address-space cap (or a stricter inherited cap), cannot write files, and cannot emit core dumps. The parent retains only bounded output. On an environment where that cap cannot support the installed modules, the adapter fails safely and leaves the schematic usable.

One image worker allows at most two unfinished jobs; identical live requests coalesce. Queue intent, worker PID/start token and active publication state are durable. Restart status distinguishes interrupted work from completion; uncertain paid requests are never automatically retried. Complete publishing entries can be recovered only when both derivatives match their recorded hashes. Incomplete/corrupt/unknown partials remain counted and preserved; they may block further cache writes rather than being silently deleted. Shutdown cancels jobs that have not started and waits for the bounded active request.

The cache retains one JPEG display derivative (up to 1,024 × 1,024), a thumbnail (up to 320 × 320), and metadata. Original/base64 provider images remain only in bounded memory. Decoding accepts at most 8 MB and 2 million pixels; animation and unsupported formats are rejected. The cache counts allocated/logical file bytes, hidden partials and metadata against **250,000,000 bytes**. It reserves 12 MB conservative incremental cache headroom before generation, plus shared storage reservation margin and cost-ledger headroom. The shared Budget adds at least 25% safety margin and protects checkpoint room. The hard total and filesystem reserve remain unchanged.

Keys include stable candidate identity, exact effective position and CRS, direction/FOV, source/geometry versions, sample-evidence hash, solar class, season, qualified weather, model and prompt version. They exclude origin, free text, score, travel convenience and exact seconds. Nearby candidates are not interchangeable. Read access updates LRU metadata when storage permits; a busy writer/low disk leaves existing read-only images usable without that timestamp update. Only hash-verified, manifest-owned disposable image derivatives can be evicted; deletion intent/history is recorded. No acquired inputs are cache members. Pins are not supported, so there is no unbounded pinned set.

## Interfaces and actual validation

`PrototypeAI(config, budget, runtime_root)` exposes `status`, `compare`, `image_key`, `generate_image` and `cached_image`. `ImageJobs(provider)` exposes `submit`, `status` and `close`. The server uses `/api/images` for submission, `/api/images/{key}` for job state and `/api/images/{key}.jpg` / `.thumb.jpg` for content. Only cached server-side view IDs can be submitted, not arbitrary prompts or URLs.

The synthetic suite exercises no-key mode, spending concurrency/restart, stale/model-mismatched prices, kill switches, schema and ID validation, prompt-injection exclusion, denominators, failure/no-retry behavior, cache identity/invalidation, image dimensions/encoding, shared storage refusal, symlinks, partial accounting, owned-only LRU eviction, recovery, queue coalescing and absolute child timeout. Run it with:

```bash
bash scripts/prototype/python.sh -m pytest tests/test_prototype_ai.py -q -p no:cacheprovider --basetemp=data/citywide/staging/prototype-provider-validation
```

Use a fresh accounted `--basetemp` for each recorded run. The first development invocation had 18 fixture-setup errors because the chosen parent directory did not exist; no provider call ran. Creating that accounted directory fixed setup. Later targeted results are recorded in the task validation report. Live text/image calls remain untested because neither credentials nor positive spending authorization was supplied. Deployment licensing remains a separate unresolved gate.
