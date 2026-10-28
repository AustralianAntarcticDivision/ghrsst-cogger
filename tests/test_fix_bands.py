import copy

import pytest

from ghrsst.cogger import SEA_ICE_UNIT, GHRSSTException
from ghrsst.fix_bands import fix_raster_bands


def band(**fields):
    return {"raster:bands": [{"nodata": -32768, "data_type": "int16", **fields}]}


def kelvin_item():
    """An item as published before 0.13.1, after 2014 (no sea_ice_fraction unit)."""
    return {
        "id": "20250101090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.stac-item",
        "properties": {"updated": "2025-01-10T00:00:00Z"},
        "assets": {
            "analysed_sst": {
                **band(scale=0.001, offset=298.15, unit="kelvin"),
                "file:checksum": "1220ab",
            },
            "analysis_error": band(scale=0.01, offset=0.0, unit="kelvin"),
            "sst_anomaly": band(scale=0.001, offset=0.0, unit="kelvin"),
            "sea_ice_fraction": band(scale=0.01, offset=0.0),
            "mask": band(),
        },
    }


def bands(doc):
    return {k: a["raster:bands"][0] for k, a in doc["assets"].items()}


def test_fix_raster_bands():
    original = kelvin_item()
    doc = fix_raster_bands(copy.deepcopy(original))

    b = bands(doc)
    assert (b["analysed_sst"]["unit"], b["analysed_sst"]["offset"]) == ("celsius", 25.0)
    assert b["analysed_sst"]["scale"] == 0.001
    assert b["analysis_error"]["unit"] == b["sst_anomaly"]["unit"] == "celsius"
    assert b["sea_ice_fraction"]["unit"] == SEA_ICE_UNIT
    assert b["mask"] == bands(original)["mask"]
    # Nothing outside the bands changes
    assert doc["properties"] == original["properties"]
    assert doc["assets"]["analysed_sst"]["file:checksum"] == "1220ab"

    # Idempotent
    assert fix_raster_bands(copy.deepcopy(doc)) == doc


def test_fix_raster_bands_keeps_existing_sea_ice_unit_and_no_anomaly():
    doc = kelvin_item()
    del doc["assets"]["sst_anomaly"]
    doc["assets"]["sea_ice_fraction"]["raster:bands"][0]["unit"] = "other"
    fix_raster_bands(doc)
    assert "sst_anomaly" not in doc["assets"]
    assert bands(doc)["sea_ice_fraction"]["unit"] == "other"


def test_fix_raster_bands_refuses_unexpected_metadata():
    doc = kelvin_item()
    doc["assets"]["analysed_sst"]["raster:bands"][0]["offset"] = 273.15
    with pytest.raises(GHRSSTException):
        fix_raster_bands(doc)
