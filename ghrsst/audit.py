"""Classify every COG as preliminary or final, without writing anything.

Same rules as `process_date(audit=True)`, with bulk lookups where possible: one S3
listing for the COG write times, and one CMR query per month for the NetCDF
production times. The NetCDF title is read per date from the archived file itself
(header only, via HTTP range requests), and only where needed.

Each row is written to the CSV as soon as it's done, so a failed run keeps its progress.
Re-running with the same --csv resumes: dates already in it are skipped, except
title_unknown ones, which are retried. The last row for a date wins.

Needs EARTHDATA_USERNAME and EARTHDATA_PASSWORD (or EARTHDATA_TOKEN) to read titles.
"""

import csv
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import click
from s3path import S3Path

from ghrsst.cogger import (
    GHRSSTException,
    _public_s3,
    classify,
    get_headers,
    get_logger,
    get_production_times,
    get_source_published,
    get_source_title,
)

LOGGER = get_logger(__name__)
PROD_LOCATION = "s3://us-west-2.opendata.source.coop/ausantarctic/ghrsst-mur-v2/"
COG_SUFFIX = "_analysed_sst.tif"
FIELDS = [
    "date",
    "cog_written",
    "source_produced",
    "source_published",
    "source_title",
    "cog_lag_days",
    "source_lag_days",
    "status",
    "reason",
]
# Statuses likely caused by a transient failure, so redone when resuming
RETRY_STATUSES = {"title_unknown", "published_unknown", "no_cmr_record"}


def list_cog_times(location: S3Path) -> dict[date, datetime]:
    """LastModified of each date's analysed_sst COG, from one S3 listing."""
    cogs: dict[date, datetime] = {}
    prefix = location.key.rstrip("/") + "/"
    pages = (
        _public_s3()
        .get_paginator("list_objects_v2")
        .paginate(Bucket=location.bucket, Prefix=prefix)
    )
    for page in pages:
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            if name.endswith(COG_SUFFIX):
                day = datetime.strptime(name[:8], "%Y%m%d").date()
                cogs[day] = obj["LastModified"]
    return cogs


def production_times(start: date, end: date) -> dict[date, datetime]:
    """CMR production times for the whole range, one small query per month."""
    times: dict[date, datetime] = {}
    month = start.replace(day=1)
    while month <= end:
        next_month = (month + timedelta(days=32)).replace(day=1)
        if month.month == 1 or month == start.replace(day=1):
            LOGGER.info(f"Querying CMR for {month:%Y}")
        times |= get_production_times(
            max(start, month), min(end, next_month - timedelta(days=1))
        )
        month = next_month
    return times


def read_rows(csv_path: Path) -> dict[str, dict[str, str]]:
    """Rows from a previous run, keyed by date. Later rows for a date replace earlier."""
    if not csv_path.exists():
        return {}
    with csv_path.open(newline="") as f:
        return {row["date"]: row for row in csv.DictReader(f)}


def write_rows(csv_path: Path, rows: dict[str, dict[str, str]]) -> None:
    """Rewrite the CSV sorted, one row per date, in the current FIELDS format.

    Columns no longer in FIELDS (e.g. from an older run) are dropped.
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows[key] for key in sorted(rows))


def _parse(value: str) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _lag(later: datetime | None, day: date) -> str:
    return str((later.date() - day).days) if later else ""


@click.command("ghrsst-audit")
@click.option("--start", type=str, default="2002-06-01", help="First date (YYYY-MM-DD)")
@click.option(
    "--end", type=str, default=None, help="Last date (YYYY-MM-DD), default today"
)
@click.option(
    "--output-location", type=str, default=PROD_LOCATION, help="COG location on S3"
)
@click.option(
    "--csv", "csv_path", type=str, default="data/audit.csv", help="CSV to write"
)
def main(start, end, output_location, csv_path):
    start = datetime.strptime(start, "%Y-%m-%d").date()
    end = datetime.strptime(end, "%Y-%m-%d").date() if end else datetime.now(UTC).date()
    location = S3Path(output_location.replace("s3://", "/"))
    csv_path = Path(csv_path)

    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    rows = read_rows(csv_path)
    # Re-apply the current rules to rows from earlier runs, using their stored values
    # (no network). Rows needing a title they don't have come out title_unknown and
    # are redone below
    for row in rows.values():
        row["status"], row["reason"] = classify(
            date.fromisoformat(row["date"]),
            _parse(row["cog_written"]),
            _parse(row["source_produced"]),
            lambda row=row: _parse(row.get("source_published", "")),
            lambda row=row: row.get("source_title") or None,
        )
    todo = [
        day
        for day in days
        if day.isoformat() not in rows
        or rows[day.isoformat()]["status"] in RETRY_STATUSES
    ]
    if rows:
        LOGGER.info(
            f"Resuming from {csv_path}: {len(days) - len(todo)} dates done, "
            f"{len(todo)} to do"
        )
        # Normalise first, so appended rows match the header even if the format changed
        write_rows(csv_path, rows)

    if todo:
        LOGGER.info(f"Listing COGs in {output_location}")
        cogs = list_cog_times(location)
        try:
            produced = production_times(todo[0], todo[-1])
            headers = get_headers()
        except GHRSSTException as e:
            raise click.ClickException(str(e)) from e

        def audit_day(day: date) -> dict[str, str]:
            # Filled only if classify needs them
            published: list[datetime | None] = []
            titles: list[str | None] = []

            def source_published() -> datetime | None:
                published.append(get_source_published(day))
                return published[-1]

            def source_title() -> str | None:
                titles.append(get_source_title(day, headers))
                return titles[-1]

            status, reason = classify(
                day, cogs.get(day), produced.get(day), source_published, source_title
            )
            return {
                "date": day.isoformat(),
                "cog_written": _iso(cogs.get(day)),
                "source_produced": _iso(produced.get(day)),
                "source_published": _iso(published[0]) if published else "",
                "source_title": (titles[0] or "") if titles else "",
                "cog_lag_days": _lag(cogs.get(day), day),
                "source_lag_days": _lag(produced.get(day), day),
                "status": status,
                "reason": reason,
            }

        LOGGER.info(
            f"Classifying {len(todo)} dates, reading NetCDF titles from the archive"
        )
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not csv_path.exists() or csv_path.stat().st_size == 0
        with csv_path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                writer.writeheader()
            # A few parallel requests; it's a one-off, so this is gentle rather than fast
            with ThreadPoolExecutor(max_workers=8) as pool:
                for row in pool.map(audit_day, todo):
                    # Saved as it goes, so a failed run keeps its progress
                    writer.writerow(row)
                    f.flush()
                    rows[row["date"]] = row
                    if row["date"].endswith("-01"):
                        LOGGER.info(f"Classified {row['date'][:7]}")

    write_rows(csv_path, rows)

    in_range = [rows[day.isoformat()] for day in days]
    counts = Counter(row["status"] for row in in_range)
    click.echo(f"{len(in_range)} dates from {start} to {end} in {csv_path}")
    for status, count in counts.most_common():
        click.echo(f"  {status}: {count}")
    for row in in_range:
        if row["status"] in (
            "no_cmr_record",
            "missing",
            "title_unknown",
            "published_unknown",
            "too_close",
            "source_nrt_only",
        ):
            click.echo(f"  {row['status']} {row['date']}: {row['reason']}")


if __name__ == "__main__":
    main()
