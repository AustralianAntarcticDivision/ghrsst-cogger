"""One-off: make published STAC raster:bands describe the COGs, not the source (#20).

Items written before 0.13.1 copied the source NetCDF's kelvin metadata, while their
COGs say celsius (offset 25). The integers are the same, so only the metadata changes.
S3 credentials for Source.Coop are needed.
"""

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import boto3
import click

from ghrsst.cogger import (
    SEA_ICE_UNIT,
    GHRSSTException,
    _read_json,
    _write_json,
    get_output_path,
)
from ghrsst.dategen import _location

FIRST_DATE = "2002-06-01"
CELSIUS = {
    "analysed_sst": {"offset": 25.0, "unit": "celsius"},
    "analysis_error": {"unit": "celsius"},
    "sst_anomaly": {"unit": "celsius"},
}


def fix_raster_bands(doc: dict) -> dict:
    """Set celsius metadata on an item's bands, in place, and return it. Idempotent."""
    assets = doc.get("assets", {})
    for var, fields in CELSIUS.items():
        if var not in assets:
            continue
        band = assets[var]["raster:bands"][0]
        # Only kelvin or already-fixed metadata is safe to relabel
        if band.get("unit") not in ("kelvin", "celsius") or (
            var == "analysed_sst" and band.get("offset") not in (298.15, 25.0)
        ):
            raise GHRSSTException(f"Unexpected {var} band in {doc.get('id')}: {band}")
        band.update(fields)
    if "sea_ice_fraction" in assets:
        assets["sea_ice_fraction"]["raster:bands"][0].setdefault("unit", SEA_ICE_UNIT)
    return doc


@click.command("fix-bands")
@click.option("--start", type=str, default=FIRST_DATE, show_default=True)
@click.option(
    "--end", type=str, default=None, help="Last date (YYYY-MM-DD), default today UTC"
)
@click.option(
    "--output-location",
    type=str,
    default="s3://ausantarctic/ghrsst-mur-v2",
    show_default=True,
)
@click.option("--dry-run", is_flag=True, help="Count what would change, write nothing")
def main(start, end, output_location, dry_run):
    start = datetime.strptime(start, "%Y-%m-%d")
    end = (
        datetime.strptime(end, "%Y-%m-%d")
        if end
        else datetime.now(UTC).replace(tzinfo=None)
    )
    dates = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    location = _location(output_location)

    def fix(date: datetime) -> str:
        stac_file = get_output_path(location, date, ".stac-item.json")
        doc = _read_json(stac_file)
        if doc is None:
            return "missing"
        before = copy.deepcopy(doc)
        if fix_raster_bands(doc) == before:
            return "unchanged"
        if not dry_run:
            _write_json(stac_file, doc)
        return "fixed"

    # Create the default session's client before the threads do: its setup isn't
    # thread-safe
    boto3.client("s3")
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(fix, dates))
    counts = {r: results.count(r) for r in ("fixed", "unchanged", "missing")}
    click.echo(f"{'Would fix' if dry_run else 'Fixed'}: {counts}")


if __name__ == "__main__":
    main()
