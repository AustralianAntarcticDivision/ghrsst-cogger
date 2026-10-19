"""Classify every COG as preliminary or final, without writing anything.

Same rules as `process_date(audit=True)`, but with bulk lookups: one S3 listing for the
COG and STAC write times, and one CMR query per year for the NetCDF production times.
"""

import csv
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import click
from s3path import S3Path

from ghrsst.cogger import (
    GHRSSTException,
    _public_s3,
    classify,
    get_logger,
    get_production_times,
)

LOGGER = get_logger(__name__)
PROD_LOCATION = "s3://us-west-2.opendata.source.coop/ausantarctic/ghrsst-mur-v2/"
COG_SUFFIX = "_analysed_sst.tif"
STAC_SUFFIX = ".stac-item.json"


def list_write_times(
    location: S3Path,
) -> tuple[dict[date, datetime], dict[date, datetime]]:
    """LastModified of each date's analysed_sst COG and STAC item, from one S3 listing."""
    cogs: dict[date, datetime] = {}
    stacs: dict[date, datetime] = {}
    prefix = location.key.rstrip("/") + "/"
    pages = (
        _public_s3()
        .get_paginator("list_objects_v2")
        .paginate(Bucket=location.bucket, Prefix=prefix)
    )
    for page in pages:
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            for suffix, times in ((COG_SUFFIX, cogs), (STAC_SUFFIX, stacs)):
                if name.endswith(suffix):
                    day = datetime.strptime(name[:8], "%Y%m%d").date()
                    times[day] = obj["LastModified"]
    return cogs, stacs


def production_times(start: date, end: date) -> dict[date, datetime]:
    """CMR production times for the whole range, one query per calendar year."""
    times: dict[date, datetime] = {}
    for year in range(start.year, end.year + 1):
        times |= get_production_times(
            max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
        )
    return times


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

    LOGGER.info(f"Listing COGs and STAC items in {output_location}")
    cogs, stacs = list_write_times(location)
    LOGGER.info(f"Querying CMR for {start} to {end}")
    try:
        produced = production_times(start, end)
    except GHRSSTException as e:
        raise click.ClickException(str(e)) from e

    rows = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        status, reason = classify(day, cogs.get(day), produced.get(day))
        rows.append(
            {
                "date": day.isoformat(),
                "cog_written": _iso(cogs.get(day)),
                "stac_written": _iso(stacs.get(day)),
                "source_produced": _iso(produced.get(day)),
                "cog_lag_days": _lag(cogs.get(day), day),
                "source_lag_days": _lag(produced.get(day), day),
                "status": status,
                "reason": reason,
            }
        )

    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(row["status"] for row in rows)
    click.echo(f"Wrote {len(rows)} dates to {csv_path}")
    for status, count in counts.most_common():
        click.echo(f"  {status}: {count}")
    for row in rows:
        if row["status"] in ("unsure", "missing"):
            click.echo(f"  {row['status']} {row['date']}: {row['reason']}")


if __name__ == "__main__":
    main()
