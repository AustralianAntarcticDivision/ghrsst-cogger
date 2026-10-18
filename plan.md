# Plan: preliminary → final COGs (issue #5)

## Glossary

- **MUR**: Multi-scale Ultra-high Resolution SST, JPL's daily global 1 km sea surface
  temperature analysis (GHRSST L4 product `MUR-JPL-L4-GLOB-v4.1`). It's the NetCDF we
  convert to COGs.
- **CMR**: NASA's Common Metadata Repository, the Earthdata catalogue API. It holds a metadata
  record per MUR file (granule), including `DataGranule.ProductionDateTime`, the time
  JPL generated that file. It's a public JSON API, so no login is needed.
- **Preliminary / final**: MUR publishes each date twice. A near-real-time file comes out
  about 1 day after the date. JPL replaces it in place (same URL and filename) with the final
  analysis 9 days after the date. Mike's audit in #5 measured that lag as 9 days at every
  quantile for 2024 onwards.

## Context

The daily job converts the last 7 days and skips any date that already has a STAC item. So
every date it ingests stays at its preliminary version. The final file arrives later, but we
never re-convert it.

Sampling COG `Last-Modified` dates on source.coop shows:

- The bulk run (11 Feb 2025, plus a partial rerun on 20 Mar 2025) wrote everything up to
  about **2025-02-01** well after day 9, so those dates are **final**.
- From about **2025-02-02** onwards, the daily job wrote each date 1–2 days after it (e.g.
  2026-08-01 written 3 Aug 2026), so those dates are **preliminary**.
- **Estimate: about 625 dates need reprocessing** (2025-02-02 to today). PR 3 produces the
  exact count.

Agreed with Mike: ingest the preliminary file as now, re-convert once the final file has
landed, and record whether each item is preliminary or final.

The signal to use is CMR `ProductionDateTime`. It only changes when the data file is
regenerated. Don't use CMR `revision-date` or the revision count, because bulk metadata
edits change them. Rule: **if the COG was written before the NetCDF's production time, it's
out of date.**

## How it runs today

There's no new infrastructure: the same two Lambda functions and SQS queue, with changed
logic.

```
EventBridge cron 00:00 UTC
  → ghrsst-lambda-daily  (dategen.py: queues one message per date)
  → SQS ghrsst-queue (+ dead-letter queue)
  → ghrsst-lambda        (cogger.py, 1 message per invocation, 10 GB, ~5 min per conversion)
      → writes 5 COGs + 1 STAC item JSON per date to source.coop
EventBridge cron 01:00 UTC
  → ghrsst-lambda-parquet-daily  (create_parquet.py: rebuilds the GeoParquet from all STAC items)
```

## How items get marked preliminary or final

The COGs themselves carry no status. Each date has one STAC item
(`YYYY/MM/DD/<file>.stac-item.json`) that lists the 5 COG assets. Its properties today
are only datetime and `proj:*`. Two properties will be added to it:

- `ghrsst:status`: `preliminary` | `final`
- `ghrsst:source_production_datetime`: the CMR `ProductionDateTime` of the NetCDF that
  was converted

The daily GeoParquet is built from these STAC items, so users can filter on
`ghrsst:status` without opening any COGs.

Rule: the status is `final` if `production - date >= 5 days`, otherwise `preliminary`. The
5-day threshold sits between the ~1-day preliminary lag and the 9-day final lag. If a final
file's lag isn't 9 days, log a warning, because it may mean JPL has changed how it publishes.

## Running cost: is ~2x realistic?

### Current costs, Apr–Sep 2026

| Service | $/month | Notes |
|---|---|---|
| S3 | 3.50–5.80 (falling) | Not the COGs: those live in source.coop's bucket and are billed to them. Likely the Terraform state bucket and the `idea-ghrsst-testing` bucket. |
| Secrets Manager | 2.00 | 4 secrets × $0.40, plus API calls |
| Tax | 0.60–0.80 | |
| ECR | 0.36 | Stored Docker images |
| Lambda | **~0.001** | Inside the free tier |
| SQS, CloudWatch | 0 | |
| **Total** | **6.50–9.00** | Lower than the $10–12 quoted in the meeting |

Lambda is effectively free. One conversion a day is about 10 GB × 300 s × 30 days ≈ 90k
GB-seconds a month, well under the free tier of 400k GB-seconds and 1M requests a month.

### After this change

- **Monthly:** two conversions a day is about 180k GB-seconds a month, still inside the free
  tier. The 14 daily checks are a few seconds each.
  - The COG writes and HEADs go to source.coop's bucket, and CMR is free.
  - **Expected change to the bill: about $0/month.** The "2x" is real for compute, but 2x of
    roughly nothing is still roughly nothing.
  - If the free tier ever stopped applying, conversion compute would be about $0.04 per
    conversion: about $1.20/month today, rising to about $2.40/month.
- **One-off backfill:** about 625 conversions × 3,000 GB-seconds ≈ 1.9M GB-seconds. After the
  free tier that's about **$20** if run in a single month. The ~8,000 patch-only invocations
  add under $1.
  - Splitting the backfill across two calendar months uses two months of free tier, which
    cuts this to about $15.
- **Verify:** compare the Cost Explorer Lambda line for the month of the backfill and the
  month after against this table.

Out of scope, but these are the actual cost drivers: S3 (find which bucket it is; the
test bucket can probably be emptied) and Secrets Manager (could be one JSON secret instead
of 4).

---

## PR 1: Move to `pyproject.toml` + `uv.lock`

- Move `requirements.in` into `[project.dependencies]` in a new `pyproject.toml`. Keep
  `--no-binary rasterio` as `[tool.uv] no-binary-package = ["rasterio"]`.
- Run `uv lock` to create `uv.lock`, then delete `requirements.in` and `requirements.txt`.
- Dockerfile: install `uv` and run `uv sync --frozen --no-dev`, or
  `uv export --frozen > /tmp/req.txt && pip install -r`, whichever keeps the GDAL base image
  happy. Keep the separate rasterio-from-source step.
- Add the dev dependencies `pytest` and `ruff` to the `dev` group.
- **Check:** `make build` succeeds, `uv run pytest` passes, and the image runs `make run-local`.

## PR 2: Format and lint

- Add a `[tool.ruff]` section to `pyproject.toml` with the defaults plus import sorting
  (`I`).
- Run `ruff format` and `ruff check --fix` across `ghrsst/` and `tests/`. Fix the remaining
  warnings by hand. This PR makes formatting changes only, with no behaviour changes.
- Add `make lint` / `make format` targets.
- **Check:** `ruff check` is clean and `uv run pytest` passes.

## PR 3: Count preliminary vs final (read-only)

Add `ghrsst/audit.py` with a click CLI. It changes nothing; it only reports.

1. **COG write times:** one paginated `list_objects_v2` over `ausantarctic/ghrsst-mur-v2/`,
   keeping the `LastModified` of each `*_analysed_sst.tif`. This takes about 9 list calls,
   not 8,000 HEADs.
2. **Production times:** `cmr_production_times(start, end) -> dict[date, datetime]`, which
   queries CMR `granules.umm_json?short_name=MUR-JPL-L4-GLOB-v4.1` one year per request with
   `page_size=2000`.
   - Uses `requests`, which is already a dependency.
   - Deduplicates on `GranuleUR`.
   - Takes about 25 requests for the full record.
3. **Join on date** and classify each date:
   - `final`: the COG was written after the production time, and the production lag is 5
     days or more.
   - `preliminary`: the COG was written before the production time (out of date).
   - `missing`: there's a NetCDF in CMR but no COG.
4. Write a CSV (`date, cog_mtime, production, lag_days, status`) and print the counts. Also
   flag any production lag other than 9 days.
- `cmr_production_times` and `get_status` live here and are reused by PR 4.
- **Run it and post the counts on #5** before PR 4. This answers "how many need
  reprocessing" exactly.
- **Test:** `tests/test_audit.py` checks that `get_status` returns `preliminary` for a
  1-day lag and `final` for a 9-day lag, and that a saved CMR JSON response parses.

## PR 4: Cogger confirms preliminary → final

### `ghrsst/cogger.py`
- `write_stac`: add `ghrsst:status` and `ghrsst:source_production_datetime`.
- `process_date`: replace the "STAC exists → skip" check with:
  1. Get the CMR production time for the date **before** downloading. If the file is
     replaced mid-run, the date is just redone next time; a preliminary file is never
     labelled final.
  2. No STAC item → convert in full.
  3. STAC says `final` and `--overwrite` isn't set → skip.
  4. STAC has a `source_production_datetime` → re-convert with `overwrite=True` only if CMR's
     production time is newer.
  5. Existing STAC with no status → compare the COG's `LastModified` (`head_object`) with the
     production time:
     - COG older → re-convert with `overwrite=True`.
     - Otherwise → **patch only the STAC JSON** with the two properties; don't touch the
       COGs.
  - Re-conversion must pass `overwrite=True` through to `write_data`, which skips existing
    COGs per variable otherwise.

### `ghrsst/dategen.py`
- Queue the last 14 days instead of 7. This covers the ingest at day 1–2 and the confirm at
  day 9–10, with slack for missed runs. A date that's already final exits after one S3 GET
  and one CMR request.
- Fix: wrap `N_PREVIOUS_DAYS` in `int(...)`. It comes from the environment as a string,
  which breaks `range`.
- SQS `send_messages` accepts at most 10 messages per call, so send in batches of 10.
- Add a `--start/--end` CLI that queues an arbitrary date range, for the backfill.
- Terraform sets `SQS_QUEUE` but the code reads `QUEUE_NAME`; it only works because the
  defaults match. Change `terraform/main.tf` to set `QUEUE_NAME`.

### Backfill
- Deploy, then queue the full record (`2002-06-01 .. today`) with the new CLI. The Lambda
  then patches already-final items and re-converts the ~625 preliminary ones.
- SQS keeps messages for 1 day; ~8,500 messages finish well within that. Afterwards, check
  the dead-letter queue and re-queue any failures.
- Re-run the PR 3 audit: it should show 0 `preliminary` items older than 10 days.

### Docs
- `readme_for_source_coop.md`: describe the two properties and the ~10-day preliminary
  window, then publish with `make update-readme`.
- `README.md`: update the daily-process description and the cost section.

### Verification
1. **Local end-to-end run** (to `data/output`):
   - A date less than 9 days old gets `preliminary`.
   - An old date gets `final`, and a rerun skips it.
   - A STAC item with the new properties removed by hand gets patched without rewriting
     its COGs.
2. **Test bucket** (`test.tfvars`): queue about 30 days that span preliminary and final, and
   check the GeoParquet counts.
3. **Production after the backfill:**
   - The audit is clean and the dead-letter queue is empty.
   - Spot checks: 2025-12-10 is re-converted, and 2024-08-01 is patched as final.
