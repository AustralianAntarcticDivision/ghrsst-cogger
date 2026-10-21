"""Our conversion and finalise code on real inputs (the sample NetCDF, real files)."""

import hashlib
import json
import os
from datetime import UTC, datetime

import rasterio
from odc.geo.xr import xr_coords

from ghrsst.cogger import (
    FILE_EXTENSION,
    FILE_STRING,
    JPL_BASE,
    PROCESSING_EXTENSION,
    STATUS_PROPERTY,
    VARIABLES,
    _attrs_as_str,
    clear_cache,
    get_cog_write_times,
    get_cogs_written,
    get_output_path,
    process_data,
    process_date,
    source_provenance,
    stac_is_stale,
    status_from_title,
    write_data,
    write_stac,
)
from tests.conftest import SAMPLE_DATE


def test_write_cogs_and_stac_from_sample(sample, tmp_path):
    # A small crop of the real sample keeps this fast while running the real code.
    # Reset the coordinates (as process_data does) so the grid metadata matches
    full = process_data(sample)
    crop = full.odc.geobox[:512, :512]
    processed = full.isel(lat=slice(0, 512), lon=slice(0, 512)).assign_coords(
        xr_coords(crop)
    )
    written = write_data(processed, SAMPLE_DATE, tmp_path, overwrite=True)

    assert [var for var, _, _ in written] == VARIABLES
    for _var, path, fields in written:
        content = path.read_bytes()
        assert fields["file:size"] == len(content)
        assert fields["file:checksum"] == "1220" + hashlib.sha256(content).hexdigest()
        with rasterio.open(path) as cog:
            assert cog.compression.name.lower() == "zstd"
            assert cog.shape == (512, 512)

    attrs = _attrs_as_str(sample.attrs)
    status = status_from_title(SAMPLE_DATE.date(), attrs["title"])
    assert status == "final"  # the sample is the final product
    provenance = source_provenance(None, None, attrs, converting=True)
    write_stac(sample, written, SAMPLE_DATE, tmp_path, status, provenance)

    stac_file = get_output_path(tmp_path, SAMPLE_DATE, ".stac-item.json")
    doc = json.loads(stac_file.read_text())
    props = doc["properties"]
    assert props[STATUS_PROPERTY] == "final"
    assert props["processing:lineage"] == "Daily MUR SST, Final product"
    assert props["processing:version"] == "04.1"
    assert props["processing:level"] == "L4"
    assert "GDAL" in props["processing:software"]
    assert "updated" in props
    assert {PROCESSING_EXTENSION, FILE_EXTENSION} <= set(doc["stac_extensions"])
    assert {
        "rel": "derived_from",
        "href": JPL_BASE
        + f"{SAMPLE_DATE:%Y%m%d}090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.nc",
    }.items() <= next(
        link for link in doc["links"] if link["rel"] == "derived_from"
    ).items()
    for var, _, fields in written:
        assert doc["assets"][var]["file:checksum"] == fields["file:checksum"]

    # Without overwrite, existing COGs are kept as they are, so no new checksums
    mtimes = {var: path.stat().st_mtime for var, path, _ in written}
    again = write_data(processed, SAMPLE_DATE, tmp_path, overwrite=False)
    assert all(fields == {} for _, _, fields in again)
    assert {var: path.stat().st_mtime for var, path, _ in again} == mtimes


def test_process_date_skips_final(tmp_path):
    # A date whose STAC item already says final stops before any lookup or download
    stac_file = get_output_path(tmp_path, SAMPLE_DATE, ".stac-item.json")
    stac_file.parent.mkdir(parents=True)
    stac_file.write_text(json.dumps({"properties": {STATUS_PROPERTY: "final"}}))

    assert process_date(SAMPLE_DATE, "data", tmp_path) is None
    assert list(stac_file.parent.iterdir()) == [stac_file]


def test_get_cogs_written_uses_oldest_existing_cog(tmp_path):
    def cog(var: str):
        return get_output_path(tmp_path, SAMPLE_DATE, f"_{var}.tif")

    old = datetime(2025, 3, 20, 14, 8, tzinfo=UTC)
    new = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    cog("analysed_sst").parent.mkdir(parents=True)
    for var in VARIABLES:
        cog(var).touch()
        when = new if var == "analysed_sst" else old
        os.utime(cog(var), (when.timestamp(), when.timestamp()))

    # A partly rewritten date: analysed_sst is new, the rest old
    assert get_cogs_written(tmp_path, SAMPLE_DATE) == old
    # Older dates have no sst_anomaly COG
    cog("sst_anomaly").unlink()
    assert get_cogs_written(tmp_path, SAMPLE_DATE) == old
    # No analysed_sst: the COGs are missing
    cog("analysed_sst").unlink()
    assert get_cogs_written(tmp_path, SAMPLE_DATE) is None


def test_stac_is_stale_when_cogs_written_after_it(tmp_path):
    # A run that wrote the COGs and stopped before the STAC item
    stac_file = get_output_path(tmp_path, SAMPLE_DATE, ".stac-item.json")
    stac_file.parent.mkdir(parents=True)
    stac_file.write_text("{}")
    stac_written = datetime(2025, 3, 20, 14, 8, tzinfo=UTC)
    os.utime(stac_file, (stac_written.timestamp(), stac_written.timestamp()))
    for var in VARIABLES:
        get_output_path(tmp_path, SAMPLE_DATE, f"_{var}.tif").touch()  # now

    cog_times = get_cog_write_times(tmp_path, SAMPLE_DATE)
    assert stac_is_stale(cog_times, stac_written)
    # The normal order: the STAC item is written after its COGs
    assert not stac_is_stale(cog_times, datetime.now(UTC))
    assert not stac_is_stale(cog_times, None)


def test_clear_cache_removes_only_mur_netcdfs(tmp_path):
    # Leftovers from failed runs: a full download and an interrupted one
    left = [
        tmp_path / FILE_STRING.format(date=SAMPLE_DATE),
        tmp_path / (FILE_STRING.format(date=datetime(2025, 2, 2)) + ".part"),
    ]
    other = [tmp_path / "other.nc", tmp_path / "notes.txt"]
    for path in left + other:
        path.write_bytes(b"x")

    clear_cache(tmp_path)

    assert sorted(tmp_path.iterdir()) == sorted(other)
