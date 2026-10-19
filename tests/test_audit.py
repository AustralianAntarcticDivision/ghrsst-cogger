from datetime import UTC, date, datetime

from ghrsst.cogger import classify, parse_production_times

DAY = date(2025, 12, 10)


def at(day: int, month: int = 12, year: int = 2025) -> datetime:
    return datetime(year, month, day, 8, tzinfo=UTC)


def test_cog_older_than_netcdf_is_preliminary():
    # Written at +2, final NetCDF produced at +9
    assert classify(DAY, at(12), at(19)) == (
        "preliminary",
        "NetCDF regenerated after COG",
    )


def test_cog_from_final_netcdf_is_final():
    assert classify(DAY, at(25), at(19))[0] == "final"


def test_reprocessed_old_date_is_final():
    # 2002 granule reprocessed in 2015, COG written in 2025
    old = date(2002, 6, 1)
    assert classify(old, at(20, 3, 2025), at(19, 8, 2015))[0] == "final"


def test_cog_from_nrt_netcdf_is_preliminary():
    # The NetCDF itself hasn't been replaced by the final yet
    assert classify(DAY, at(12), at(11)) == (
        "preliminary",
        "COG written from NRT NetCDF",
    )


def test_cog_without_cmr_record():
    assert classify(DAY, at(12), None)[0] == "no_cmr_record"


def test_no_cog_is_missing():
    assert classify(DAY, None, at(19))[0] == "missing"
    assert classify(DAY, None, None)[0] == "no_cmr_granule"


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
