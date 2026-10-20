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
- **Estimate: about 625 dates need reprocessing** (2025-02-02 to today). PR 5 produces the
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

## PR 3: CI — build, test and push the image

GitHub Actions, two workflows.

**On every PR and push to `main`** (`.github/workflows/ci.yml`):

- `uv sync --frozen`, then `ruff check` and `ruff format --check`.
- Build the `test` Docker stage and run it (`make test-docker`). Tests that need the sample
  NetCDF skip, because downloading it needs Earthdata credentials.
- Build the `final` stage. Its build step imports all three Lambda handlers, which catches
  a broken image before it reaches ECR. Nothing is pushed.
- Cache Docker layers with `cache-from/cache-to: type=gha`. The dependency layer (rasterio
  from source) is the slow part and only changes when `uv.lock` does.

**On a version tag `v*`** (`.github/workflows/release.yml`):

- Build `final` for `linux/amd64` and push to ECR as `<version>` and `latest`.
- Authenticate with GitHub OIDC instead of long-lived AWS keys. Add to `terraform/main.tf`:
  - an `aws_iam_openid_connect_provider` for `token.actions.githubusercontent.com`
  - a role that only this repo can assume, with ECR push permissions on the
    `ghrsst-cogger` repository
- Check that the tag matches `version` in `pyproject.toml`, and fail otherwise.

**Deploys stay manual:** bump the image tag in `terraform/prod.tfvars`, then run `make apply`.
`apply` changes production, so it stays a deliberate step.

**Check:**

- Open a PR and confirm the CI workflow runs and passes.
- Push a test tag (for example `v0.13.0-rc1`) and confirm the image appears in ECR.
- Point `test.tfvars` at that tag and apply to the test stack.

## PR 4: Upgrade dependencies

PR 1 kept the exact versions from `requirements.txt`, so packaging and upgrades stay
separate. All 18 direct dependencies are now behind. The `>=` bounds in `pyproject.toml`
can stay as they are, because `uv.lock` pins the exact versions.

- Run `uv lock --upgrade` and review the diff in `uv.lock`.
- Remove `odc-cloud`, which nothing imports.
  - Keep dask, h5netcdf, netcdf4, rasterio and shapely. They aren't imported directly but
    are needed at runtime: as xarray backends, for chunking, or for odc-geo's COG writing.
- Upgrades most likely to break something:

  | Package | From → to | Risk |
  |---|---|---|
  | odc-geo | 0.4.9 → 0.5.x | `cogger.py` calls the private functions `odc.geo.cog._rio._write_cog` and `_get_gdal_metadata`, where the geotransform fix lives |
  | earthaccess | 0.13 → 0.19 | `login(strategy="environment")` and `get_edl_token()` for the Earthdata login |
  | awslambdaric | 3.0 → 4.x | Major version; it's the Lambda entrypoint |
  | stacrs | 0.5.3 → 0.5.9 | Stay within 0.5. The package has been renamed `rustac`; switching is a later job |
  | xarray, dask, fsspec | about 20 months behind | Loading, chunking and HTTP reads of the NetCDF |

- If something breaks, cap that package (e.g. `odc-geo<0.5`) and note why in
  `pyproject.toml`, rather than holding everything back.
- **Check:** the unit tests skip without data, so run a real conversion:
  - Convert the same date with the old (`0.13.0`) and new images.
  - The COGs should be pixel-identical.
  - The geotransform should be unchanged (`-179.995, 0.01`), and the STAC items equivalent.
  - Deploy to the test stack and run one queued date. This checks awslambdaric and the
    Earthdata login.

## PR 5: Count preliminary vs final (read-only)

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
- `cmr_production_times` and `get_status` live here and are reused by PR 6.
- **Run it and post the counts on #5** before PR 6. This answers "how many need
  reprocessing" exactly.
- **Test:** `tests/test_audit.py` checks that `get_status` returns `preliminary` for a
  1-day lag and `final` for a 9-day lag, and that a saved CMR JSON response parses.

## PR 6: Cogger confirms preliminary → final

See pr6-plan.md

# TODO: Update source coop readme. e.g. rustac update.

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
