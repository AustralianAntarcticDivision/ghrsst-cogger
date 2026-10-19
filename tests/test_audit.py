from datetime import UTC, date, datetime

import pytest

from ghrsst.cogger import classify, parse_production_times, parse_title

DAY = date(2025, 12, 10)
FINAL = "Daily MUR SST, Final product"
NRT = "Daily MUR SST, Interim near-real-time (nrt) product"


def at(day: int, month: int = 12, year: int = 2025) -> datetime:
    return datetime(year, month, day, 8, tzinfo=UTC)


def title(value: str | None):
    return lambda: value


def not_needed():
    pytest.fail("the NetCDF title shouldn't be fetched for this case")


def test_cog_older_than_netcdf_is_preliminary_without_fetching_title():
    # Written at +2, final NetCDF produced at +9
    assert classify(DAY, at(12), at(19), not_needed) == (
        "preliminary",
        "NetCDF regenerated after COG",
    )


def test_cog_from_final_netcdf_is_final():
    assert classify(DAY, at(25), at(19), title(FINAL)) == (
        "final",
        "COG written from final NetCDF",
    )


def test_reprocessed_old_date_is_final():
    # 2002 granule reprocessed in 2015, COG written in 2025
    old = date(2002, 6, 1)
    assert classify(old, at(20, 3, 2025), at(19, 8, 2015), title(FINAL))[0] == "final"


def test_early_final_is_final():
    # Finalised at +4 (as MUR does now), COG written after it
    assert classify(DAY, at(15), at(14), title(FINAL))[0] == "final"


def test_cog_from_nrt_netcdf_is_preliminary():
    # The NetCDF itself hasn't been replaced by the final yet
    assert classify(DAY, at(12), at(11), title(NRT)) == (
        "preliminary",
        "COG written from NRT NetCDF",
    )


def test_unreadable_or_unknown_title():
    assert classify(DAY, at(12), at(11), title(None)) == (
        "title_unknown",
        "Couldn't read NetCDF title",
    )
    assert classify(DAY, at(12), at(11), title("Something else")) == (
        "title_unknown",
        "Unrecognised NetCDF title",
    )


def test_cog_without_cmr_record():
    assert classify(DAY, at(12), None, not_needed)[0] == "no_cmr_record"


def test_no_cog_is_missing():
    assert classify(DAY, None, at(19), not_needed)[0] == "missing"
    assert classify(DAY, None, None, not_needed)[0] == "no_cmr_granule"


def test_parse_title_from_dmr():
    ns = "http://xml.opendap.org/ns/DAP/4.0#"
    dmr = f"""<?xml version="1.0" encoding="ISO-8859-1"?>
<Dataset xmlns="{ns}" name="x.nc">
    <Float32 name="lat">
        <Attribute name="units" type="String"><Value>degrees</Value></Attribute>
    </Float32>
    <Attribute name="title" type="String"><Value>{FINAL}</Value></Attribute>
</Dataset>"""
    assert parse_title(dmr) == FINAL
    assert parse_title(f'<Dataset xmlns="{ns}"/>') is None


def test_parse_matches_granule_ur_not_window():
    # A one-day query also returns the next day's granule; keep both, keyed by name,
    # and take the latest production time when a granule has several records
    def granule(ur: str, produced: str) -> dict:
        return {
            "umm": {"GranuleUR": ur, "DataGranule": {"ProductionDateTime": produced}}
        }

    umm_json = {
        "items": [
            granule("20251210090000-JPL-L4_GHRSST", "2025-12-11T07:00:00.000Z"),
            granule("20251210090000-JPL-L4_GHRSST", "2025-12-19T07:17:35.000Z"),
            granule("20251211090000-JPL-L4_GHRSST", "2025-12-20T07:17:44.000Z"),
        ]
    }
    times = parse_production_times(umm_json)
    assert times[DAY] == datetime(2025, 12, 19, 7, 17, 35, tzinfo=UTC)
    assert set(times) == {DAY, date(2025, 12, 11)}
