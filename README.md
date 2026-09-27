# Jev for Support Systems

A public demo that sends the same support task to OpenAI and to Jev at the same instant, streams each answer back the moment it lands, and keeps a permanent record of latency and cost for every run.

Two features, both running in production today:

- **Ticket recategorisation.** Read a customer's comment and place it in the right category (13 areas, 170 categories, 87 of them warranty faults; warranty is multi-label). If nothing fits, the answer is "no match". Both sides get only the comment and the category list.
- **Message prioritiser.** Read one customer message and decide whether the ticket is urgent: time-sensitive, an escalation or accusation, a long-unresolved issue, or a breakdown/safety problem. Everything else (thanks, greetings, callback requests, status checks) is not urgent.

**OpenAI (`gpt-4.1`) is called the way production calls it:** the production prompt with the category list as the system message, the comment as the user message (`data/recat_user_template.txt`), the production response schema, and production's temperatures (0.3 recat, 0.2 prioritiser). The prioritiser uses our own prompt (`data/prioritiser_prompt.txt`) with the same four urgency definitions Jev gets, and appends its schema to the system message as production does. **Jev** runs the most efficient design we measured, through the OpenRouter Decisions API.

**Choosing the OpenAI model.** `gpt-4.1` is the default because it's the model these features run on in production. Visitors can race Jev against `gpt-4o-mini`, `gpt-6-luna` or `gpt-6-sol` instead (`OPENAI_MODELS`); history and stats are kept per model so baselines never mix.

## How the Jev side works

**Recategorisation: one fused request with 100 questions.**

| Questions | Type | Purpose |
|---|---|---|
| `query` | choice over 13 areas + "None of these" | which support area |
| `leaf0`…`leaf12` (12 of them) | choice, one per non-warranty area | "assuming it's about X, which sub-query › heading" |
| `w0`…`w86` | yes/no, one per warranty fault, worded "Fault: <sub-query > heading>" | multi-label warranty |

Python reads `query` and keeps only the matching branch: the one `leaf{i}` answer, or every `w*` with p ≥ 0.55 for warranty. The other answers are thrown away. This is as accurate as asking one level at a time (area → sub-query → heading), but you only wait for a single round trip. A chain would triple Jev's latency.

The "My issue is not listed here" options stay in the taxonomy. Jev sees them reworded as "*<sub-query> › something else, not covered by the other options*", and the answer keys map back to the exact category. If Jev picks "None of these", the answer is "no match".

Jev bills the state (the comment) once per request, not per question, so input is ~7,000 tokens per call. Naming each warranty fault once instead of three times cut that by 19% with identical answers (`scripts/ab_warranty_wording.py`).

If a request is refused for size (400/413/422), the questions are split in half and **both halves are sent in parallel**, recursively. The run is recorded as `split-N`.

**Prioritiser: four yes/no questions**, one per urgency signal, worded exactly like the definitions in OpenAI's prompt. Urgent if any clears its threshold (0.55; 0.45 for escalation and safety, where a miss costs most); the first signal in order is reported and the reason is built in Python (`app/prioritiser.py: compose`).

## Run locally

```sh
py -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest -q
cp .env.example .env   # add both keys
.venv/Scripts/python -m uvicorn app.main:app --reload --env-file .env
```

Frontend only, with no backend and no keys: `node dev-server.mjs`, then open http://localhost:5173. With no API to call, the pages fall back to an in-browser mock and show a "Mock data" banner. Mock switches: `?mock=1`, `?mockfail=openai|jev|429|503|cap`, `?mockreset=1`.

## Data

- `data/taxonomy.json` is built from the queries spreadsheet by `scripts/build_taxonomy.py <xlsx>`, which sanitises brand names and merges casing duplicates.
- `data/presets.json` and the two prompts are edited by hand. The prompts are the production prompts with the brand name removed and `extra_details` dropped.
- After changing `data/`, run `node scripts/build_mock_data.mjs` to refresh the frontend mock.

## Deploy

### Vercel
1. Push to GitHub (`.env`, `eval/`, the POC file and `race.db` are gitignored, and `.vercelignore` keeps them out of CLI uploads too), then import the repo in Vercel.
2. **Storage → Neon Postgres**, linked to the project. Use the **pooled** connection string as `DATABASE_URL`; the `postgres://…?sslmode=require` form works as-is (`app/db.py: normalise_url`).
3. **Environment variables:** `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `DATABASE_URL`, `IP_HASH_SALT` (any long random string); optionally `DAILY_SPEND_CAP_USD`, `RATE_LIMIT_PER_HOUR`, `OPENAI_MODELS`.
4. Deploy. `api/index.py` is the entry point; `vercel.json` routes every path to it, allows 60 s, and bundles `app/`, `data/` and `static/`.
5. Check: run a race and **confirm the Jev card fills before the OpenAI card** (response streaming works); reload and redeploy and confirm history survives; trip the rate limit.

Serverless notes: tables are created on the first request (no reliance on ASGI lifespan); the per-IP limit is kept in Postgres as salted IP hashes so it holds across instances; a race whose visitor closes the tab mid-way may not be saved, because the function can be frozen once the response ends.

### Render (free web service)
1. Push to GitHub, then in Render: **New → Blueprint** and pick the repo. `render.yaml` defines the service (free plan, `pip install -r requirements.txt`, `uvicorn app.main:app`, health check `/healthz`).
2. When asked, fill in `OPENAI_API_KEY`, `OPENROUTER_API_KEY` and `DATABASE_URL`. `IP_HASH_SALT` is generated for you.
3. `DATABASE_URL` must be an external Postgres, because the free web service's disk is wiped on every restart and SQLite would lose the history.
   - **Supabase:** Project → Connect → **Session pooler** (port 5432) or **Transaction pooler** (port 6543) string, not the direct connection (free-tier direct connections are IPv6-only). Both pooler modes work; `app/db.py` turns off prepared-statement caching for them.
   - **Neon:** the pooled connection string; `postgres://…?sslmode=require` works as-is.
4. Check: a race streams the Jev card first; history survives a manual redeploy; the rate limit trips.

Free-plan behaviour: the service sleeps after ~15 minutes without traffic and takes ~30–60 s to wake. Race timings are measured on the server and aren't affected. An uptime monitor pinging `/api/stats` every 10 minutes keeps it awake, and because that route queries the database it also counts as activity for Supabase, whose free projects pause after a week without any.

Any other host that runs the Dockerfile (e.g. Railway) works the same way.

Either way, set a hard monthly budget on the OpenAI and OpenRouter dashboards as well as `DAILY_SPEND_CAP_USD`.

## Analytics

Optional and off by default. Set `UMAMI_WEBSITE_ID` (from a free Umami Cloud site) and the page loads Umami, which is cookieless and stores no personal data. It shows visitors, page views, referrers and countries, plus one event, `race_run` (feature, model, preset or "custom"), and `race_blocked` when the rate limit or budget stops a race. Events never carry text a visitor typed. Mock mode never sends analytics.

## Honesty rules

1. Timings and arrival order are measured, never scripted or padded.
2. Every cost figure says where it came from: Jev's is reported by OpenRouter; OpenAI's is computed from list price.
3. Reasoning tokens are shown separately wherever OpenAI tokens appear.
4. Failures are shown, and a race with a failure has no winner.
5. Production ran on Gemini 2.5 Flash; the demo baseline is OpenAI. Page 2 says so and shows calibration ratios.
6. No company names, and visitors' text is never stored.

## Verified with real keys (September 2026)

- Jev accepts the 100-question fused request in one call; `split-N` has not triggered.
- Jev bills the state once per request (`scripts/jev_probe.py`), and returns `usage.cost` on every response.
- `gpt-4.1` accepts strict `json_schema` and production's temperatures. (`gpt-6-luna` rejects `temperature`; the client retries without it and flags the run.)
- On the presets (`scripts/compare_presets.py`), Jev was ~9× faster and ~10× cheaper than `gpt-4.1` for recategorisation (agreeing on 7/8), and ~4× faster and ~63× cheaper for the urgency prioritiser (9/9 on urgent vs not). OpenAI latency varies a lot between runs.

