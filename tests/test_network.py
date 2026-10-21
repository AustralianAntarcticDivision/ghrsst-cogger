"""Our lookups against the real CMR, PO.DAAC archive and source.coop.

Off by default; run with `make test-network` (pytest -m network). Uses old dates whose
values are stable. The NetCDF header and finalise tests also need EARTHDATA_USERNAME and
EARTHDATA_PASSWORD (or EARTHDATA_TOKEN), and are skipped without them.
"""

import asyncio
import os
from datetime import UTC, date, datetime

import pytest
import rustac
from s3path import S3Path

from ghrsst.cogger import (
    get_cogs_written,
    get_production_times,
    get_source_attrs,
    get_source_published,
    process_date,
)
from ghrsst.create_parquet import create_parquet
from ghrsst.dategen import is_final

pytestmark = pytest.mark.network

PROD = S3Path("/us-west-2.opendata.source.coop/ausantarctic/ghrsst-mur-v2")
needs_login = pytest.mark.skipif(
    not (os.environ.get("EARTHDATA_TOKEN") or os.environ.get("EARTHDATA_PASSWORD")),
    reason="needs Earthdata credentials",
)


def test_cmr_production_time():
    day = date(2024, 8, 1)
    times = get_production_times(day, day)
    assert times[day] == datetime(2024, 8, 10, 7, 17, 2, tzinfo=UTC)
    # A one-day query also returns the next day's granule, keyed by its own date
    assert date(2024, 8, 2) in times


def test_is_published():
    from ghrsst.cogger import is_published

    assert is_published(date(2024, 8, 1)) is True
    assert is_published(date(2030, 1, 1)) is False


def test_publication_time():
    published = get_source_published(date(2025, 2, 1))
    assert published == datetime(2025, 2, 10, 7, 32, 16, tzinfo=UTC)


def test_cogs_written_on_source_coop():
    # 2002 has 4 COGs (no sst_anomaly); all written in the 2025-03-20 bulk run
    written = get_cogs_written(PROD, datetime(2002, 6, 1))
    assert written is not None and written.date() == date(2025, 3, 20)
    assert get_cogs_written(PROD, datetime(2030, 1, 1)) is None


def test_is_final_reads_source_coop():
    assert is_final(PROD, datetime(2030, 1, 1)) is False  # no STAC item
    assert isinstance(is_final(PROD, datetime(2002, 6, 1)), bool)


@needs_login
def test_source_attrs_from_netcdf_header():
    from ghrsst.cogger import get_headers

    attrs = get_source_attrs(date(2024, 8, 1), get_headers())
    assert attrs == {
        "title": "Daily MUR SST, Final product",
        "product_version": "04.1",
        "processing_level": "L4",
    }


def test_create_parquet_from_source_coop(tmp_path):
    count = asyncio.run(
        create_parquet(
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 1, 2),
            input_location="s3:/ausantarctic/ghrsst-mur-v2",
            output_location=str(tmp_path),
        )
    )
    assert count == 2

    async def read():
        return await rustac.read(str(tmp_path / "ghrsst-mur-v2.parquet"))

    items = asyncio.run(read())["features"]
    assert [i["properties"]["datetime"][:10] for i in items] == [
        "2024-01-01",
        "2024-01-02",
    ]


@needs_login
def test_finalise_sets_status_without_touching_cogs(tmp_path):
    """The Lambda's path for an old final date, on a local copy of its prod metadata."""
    import json

    from ghrsst.cogger import STATUS_PROPERTY, VARIABLES, _public_s3, get_output_path

    day = datetime(2002, 6, 1)
    s3 = _public_s3()
    # The real STAC item, and placeholder COGs with the real write times (4 for 2002)
    for ext in [f"_{v}.tif" for v in VARIABLES] + [".stac-item.json"]:
        src, dst = get_output_path(PROD, day, ext), get_output_path(tmp_path, day, ext)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            obj = s3.head_object(Bucket=src.bucket, Key=src.key)
        except s3.exceptions.ClientError:
            continue  # no sst_anomaly COG in 2002
        if ext.endswith(".json"):
            body = s3.get_object(Bucket=src.bucket, Key=src.key)["Body"].read()
            doc = json.loads(body)
            doc["properties"].pop(STATUS_PROPERTY, None)  # as before the backfill
            dst.write_text(json.dumps(doc))
        else:
            dst.touch()
        t = obj["LastModified"].timestamp()
        os.utime(dst, (t, t))
    cogs = sorted(tmp_path.rglob("*.tif"))
    before = {p: p.stat().st_mtime for p in cogs}

    process_date(day, "JPL", tmp_path)

    stac = json.loads(get_output_path(tmp_path, day, ".stac-item.json").read_text())
    assert stac["properties"][STATUS_PROPERTY] == "final"
    assert stac["properties"]["processing:lineage"] == "Daily MUR SST, Final product"
    assert {p: p.stat().st_mtime for p in cogs} == before  # COGs untouched
