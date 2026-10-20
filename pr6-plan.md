# Plan: PR 6: record preliminary/final status in STAC, finalise going forward, backfill

## Context
The audit (PR #12) derives, for every date, whether its COG was made from MUR's NRT or final NetCDF:
- First run: ~8,210 final, ~623 daily-era preliminary, 50 dates whose archived NetCDF is still NRT, and a handful unknown.
- A re-run with the publication-time rules is in progress.

Nothing on source.coop records this today, and the daily job never revisits a date (it skips anything with a STAC item), so preliminary COGs stay preliminary forever.

PR 6:
1. Adds a status (plus light provenance) to **every STAC item**, using the audit's exact rules.
2. Changes the Lambda so it **writes the status going forward** and **finalises** dates when the final NetCDF lands.
3. Makes `dategen` revisit recent dates long enough to catch finals.
4. Gets the status into the **GeoParquet**.
5. Backfills by **queueing every date**.

Decisions (agreed): richer status values plus provenance; backfill through the queue, with the Lambda re-deriving each status at processing time.

## STAC item changes
The approach follows stac-spec #179: a minimal label for discovery, a `derived_from` link to the source, and provenance in the standard [Processing extension](https://github.com/stac-extensions/processing) (v1.3.0, Candidate).

**Status label (custom, for filtering).** The Processing extension has no preliminary/final field, and `processing:lineage` is free text, so a simple filterable label is custom:
- `ghrsst:status`: `final` | `preliminary` | `nrt_only`
  - `final`: made from the final NetCDF.
  - `preliminary`: made from an NRT NetCDF whose final is still to come.
  - `nrt_only`: the archived NetCDF is still NRT more than `NRT_ONLY_AFTER` (60 days) after the date, so no final was published.

**Provenance (Processing extension).** Add `https://stac-extensions.github.io/processing/v1.3.0/schema.json` to the item's `stac_extensions`.

| Field | Value | Source |
|---|---|---|
| `processing:lineage` | the source NetCDF's own title, e.g. `"Daily MUR SST, Final product"` | NetCDF `title` attribute |
| `processing:datetime` | when JPL produced the source NetCDF | CMR `ProductionDateTime` |
| `processing:facility` | `"NASA JPL"` | constant |
| `processing:version` | the MUR product version, e.g. `"04.1"` | NetCDF `product_version` attribute |
| `processing:level` | `"L4"` | NetCDF `processing_level` attribute |
| `processing:software` | e.g. `{"ghrsst-cogger": "0.13.0", "rasterio": "1.5.2", "GDAL": "3.12.2", "odc-geo": "0.5.3"}` | `importlib.metadata` and `rasterio.__gdal_version__` |

- `processing:software` is only written when a COG is (re)made. A status-only patch can't know the versions that made the existing COG, so it leaves this field unset (or as it was).
- After the backfill, re-COGed items therefore say which software made them, which makes reprocessing traceable.

**Custom provenance with no standard field:**
- `ghrsst:source_published`: the archive `Last-Modified` of the converted NetCDF. It's when PO.DAAC published it, which the audit's rules depend on.

Links: the "Original NetCDF" link's `rel` changes from `canonical` (strictly, the authoritative copy of *this item*) to `derived_from` (the source it was made from). The title stays "Original NetCDF".

**Where the status comes from:**
- When converting: from the **downloaded file's own `title`** (`data.attrs["title"]`), so it's exact.
- When not converting: from the audit's `classify()`.

## Changes

### What each date costs (early exits)
The COG is only rewritten when that changes the data. Everything else exits early:

| Situation | What happens | COG rewritten? |
|---|---|---|
| STAC item already `ghrsst:status: final` | Exit after one S3 GET | No |
| COG already from the final NetCDF, but no status yet (most backfill dates) | Patch the STAC item only, then exit | No |
| `nrt_only`, or NRT with the final not out yet | Patch the STAC item only, then exit | No: same data |
| COG from an older file than the current one (`preliminary`/`too_close`), or no COG | Finalise: re-COG and write a new STAC item | **Yes** |

### `ghrsst/cogger.py`
- **`status_from_title(day, title, today=None) -> str | None`**: "Final product" → `final`; "near-real-time" → `nrt_only` if the date is over `NRT_ONLY_AFTER` old, else `preliminary`; otherwise `None`.
  - `classify()`'s title rules call it, so the audit and STAC writing share one mapping.
  - Rename the audit's `source_nrt_only` to match (`nrt_only`), with the same reason text.
- **Reason constants** (e.g. `REASON_NRT_SOURCE = "COG written from NRT NetCDF"`), used by `classify` and by the action mapping below, instead of repeating strings.
- **`action_for(status, reason) -> str`**: a pure function, unit-tested, mapping audit results to what the Lambda does:

  | Audit result | Action |
  |---|---|
  | `final`, `nrt_only`, or `preliminary` + NRT source (final not out yet) | `set_status`: patch the STAC item only; re-COGing would change nothing |
  | `preliminary` (NetCDF regenerated/published after COG), `too_close`, `missing` | `reprocess`: re-COG with `overwrite=True` |
  | `*_unknown`, `no_cmr_record` | `retry`: raise `GHRSSTException`, so SQS retries, then sends it to the DLQ |
  | `no_cmr_granule` | `skip`: nothing published yet |

- **`source_provenance(...) -> dict`**: builds the `processing:*` and `ghrsst:source_published` properties from the NetCDF attributes (when converting), the CMR production time and the publication time. `processing:software` is only included when converting.
- **`write_stac(..., status, provenance)`**: add `ghrsst:status`, the provenance properties, the Processing extension schema URL and the `derived_from` link. Move the S3 `put_object` and local-save code into `_write_json(stac_file, dict)`, so it's reused.
- **`set_stac_status(output_location, date, status, provenance)`**: read the existing STAC JSON (S3 `get_object` / local), set `ghrsst:status` and the provenance properties, add the extension URL, switch `canonical` → `derived_from`, and write it back with `_write_json`.
  - The lineage, version and level come from the NetCDF title and attributes, read from the file header the audit already reads (~1 MB). They're not guessed.
- **`process_date` (non-audit)**: replaces "skip if a STAC item exists":
  1. Read the existing STAC item. If its `ghrsst:status` is `final` and not `overwrite` → skip. This is one S3 GET, and it covers most daily checks.
  2. No STAC item or COG, or `overwrite` → convert. The status comes from the downloaded file's title; S comes from `get_production_times` and P from `get_source_published` (a HEAD request made **before** the download).
  3. Otherwise run `classify()` (the audit logic: S, then lazy P, then lazy T) and act per `action_for`.
  - Reprocessing reuses the existing `load_data`, `process_data`, `write_data(overwrite=True)` and `write_stac` steps.
- **`lambda_handler`**: unchanged, apart from `GHRSSTException` from a `retry` result now propagating, so SQS retries the message instead of it being dropped.

### `ghrsst/dategen.py`
- **Default `N_PREVIOUS_DAYS` → 62.** Finals usually land at +9 days, occasionally up to ~40, and `nrt_only` is decided at 60 days. Dates already `final` cost one S3 GET.
- **Send in batches of 10** (the SQS `send_messages` limit). Today's single call only works because 7 ≤ 10.
- **Backfill CLI:** `python -m ghrsst.dategen --start 2002-06-01 [--end YYYY-MM-DD]` queues every date in the range, using your local AWS credentials.

### `ghrsst/create_parquet.py`
- No code change: rustac copies STAC properties into the GeoParquet. Verify that `ghrsst:status` shows up as a column after a rebuild.

### `terraform/main.tf`
- **Concurrency cap:** event source mapping `scaling_config { maximum_concurrency = 10 }`, so ~8,900 backfill messages don't fan out into hundreds of Lambdas hitting CMR, PO.DAAC and source.coop.
- **Retention:** SQS queue and DLQ `message_retention_seconds` 1 day → 4 days (the backfill takes hours, and failures need time to be inspected).
- **Daily Lambda env:** set `QUEUE_NAME` (fixes the long-standing `SQS_QUEUE` mismatch) and `N_PREVIOUS_DAYS = 62`.
- `prod.tfvars`: `image_tag` → the release built from this PR.

### Docs
- `readme_for_source_coop.md`: document `ghrsst:status` (with its 3 values), the `processing:*` fields, `ghrsst:source_published` and `derived_from`, plus a GeoParquet filter example (e.g. `ghrsst:status = 'final'`). Publish with `make update-readme` after deploying.
- `README.md`: the finalise flow (daily look-back, status sources, backfill command).

### Tests (`tests/test_audit.py` or a new `tests/test_status.py`, network-free)
- `status_from_title` for each title and age.
- `action_for` for every status/reason.
- `set_stac_status` on a local STAC item in `tmp_path`: status and provenance set, the Processing extension URL added, `canonical` → `derived_from`, and existing `processing:software` left alone.
- `source_provenance`: maps NetCDF attributes and times to the `processing:*` fields, and only includes software when converting.
- Validate a written item against the STAC and Processing extension JSON schemas (e.g. `pystac`'s `item.validate()`).

## Rollout
1. Merge after #12. Bump `version` (e.g. `0.13.0`) and tag `v0.13.0`; the release workflow pushes the image.
2. Set `prod.tfvars` `image_tag`, then `make plan` / `make apply`. This moves the Lambdas off `0.12.2` and applies the concurrency cap, retention and env changes.
3. **Smoke test:** queue 3 dates (`--start`/`--end`):
   - a `final` one (2024-08-01 → status-only patch)
   - a stale `preliminary` one (2025-12-10 → re-COG, status `final`)
   - an `nrt_only` one (2015-04-22 → status-only patch)

   Check the STAC items and the CloudWatch logs.
4. **Backfill:** `python -m ghrsst.dategen --start 2002-06-01`, about 8,900 messages. At concurrency 10: ~8,200 status patches (~10 s each, ~2–3 h) plus ~680 re-COGs (~4 min each, ~4–5 h). Expect about 7 h in total, well inside the 4-day retention. Watch the DLQ.
5. The next day's parquet rebuild includes `ghrsst:status`. Re-run the audit to confirm there are no stale `preliminary` dates older than ~10 days.

**Cost:**
- One-off: about 680 re-COGs at ~$0.04 ≈ **$27**. Status patches are small; Lambda compute stays roughly within the free tier.
- Ongoing: 62 cheap checks a day, plus about one re-COG a day when each final lands.

## Verification
- `make lint` and `make test` (the new unit tests).
- Local end-to-end into `data/output`:
  - Convert a date → the STAC item has `ghrsst:status: final`, the `processing:*` fields, `ghrsst:source_published` and a `derived_from` link, and passes schema validation.
  - Run `process_date` again → skipped because it's `final`.
  - Strip the status from a copy → `set_status` path.
- After deploying: the smoke-test dates above, the DLQ empty after the backfill, and the GeoParquet `ghrsst:status` value counts matching the audit, with the `processing:*` columns present.
