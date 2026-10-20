from datetime import UTC, date, datetime, timedelta

import pytest

from ghrsst.cogger import classify, parse_production_times

DAY = date(2025, 12, 10)
FINAL = "Daily MUR SST, Final product"
NRT = "Daily MUR SST, Interim near-real-time (nrt) product"


def at(day: int, month: int = 12, year: int = 2025, hour: int = 8) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def value(v):
    return lambda: v


def not_needed():
    pytest.fail("this lookup shouldn't be needed for this case")


def test_cog_older_than_netcdf_production_is_preliminary_without_lookups():
    # Written at +2, final NetCDF produced at +9
    assert classify(DAY, at(12), at(19), not_needed, not_needed) == (
        "preliminary",
        "NetCDF regenerated after COG",
    )


def test_produced_before_but_published_after_cog_is_preliminary():
    # The gap ProductionDateTime alone can't see: produced at 07:17, COG at 07:20,
    # published at 07:32, so the COG was made from the previous file
    produced = at(19, hour=7)
    cog = produced + timedelta(minutes=3)
    published = produced + timedelta(minutes=15)
    assert classify(DAY, cog, produced, value(published), not_needed) == (
        "preliminary",
        "NetCDF published after COG",
    )


def test_cog_soon_after_publication_is_too_close():
    published = at(19)
    cog = published + timedelta(minutes=30)
    assert classify(DAY, cog, at(19, hour=7), value(published), not_needed) == (
        "too_close",
        "COG written within 1 h of NetCDF publication",
    )


def test_bulk_run_boundary_is_now_calculated():
    # 2025-02-01: final produced 07:16:59 and published 07:32:16 on 2025-02-10,
    # bulk-run COG 2025-02-11 02:32, 19 h after publication, so made from the final
    produced = datetime(2025, 2, 10, 7, 16, 59, tzinfo=UTC)
    published = datetime(2025, 2, 10, 7, 32, 16, tzinfo=UTC)
    cog = datetime(2025, 2, 11, 2, 32, tzinfo=UTC)
    status = classify(date(2025, 2, 1), cog, produced, value(published), value(FINAL))
    assert status == ("final", "COG written from final NetCDF")


def test_unreadable_publication_time():
    assert classify(DAY, at(25), at(19), value(None), not_needed) == (
        "published_unknown",
        "Couldn't read NetCDF publication time",
    )


def test_cog_from_final_netcdf_is_final():
    assert classify(DAY, at(25), at(19), value(at(19, hour=9)), value(FINAL)) == (
        "final",
        "COG written from final NetCDF",
    )


def test_reprocessed_old_date_is_final():
    # 2002 granule reprocessed in 2015, re-stored by PO.DAAC in 2024, COG in 2025
    old = date(2002, 6, 1)
    status = classify(
        old,
        at(20, 3, 2025),
        at(19, 8, 2015),
        value(at(17, 7, 2024)),
        value(FINAL),
    )
    assert status[0] == "final"


def test_cog_from_nrt_netcdf_is_preliminary():
    # The NetCDF itself hasn't been replaced by the final yet
    recent = date(2025, 12, 20)
    status = classify(
        DAY, at(15), at(11), value(at(11, hour=9)), value(NRT), today=recent
    )
    assert status == ("preliminary", "COG written from NRT NetCDF")


def test_old_nrt_netcdf_is_nrt_only():
    # e.g. 2015-04-22: NRT produced at +3 and never replaced by a final
    old = date(2015, 4, 22)
    produced = datetime(2015, 4, 25, 2, 43, tzinfo=UTC)
    status = classify(old, at(20, 3, 2025), produced, value(produced), value(NRT))
    assert status == (
        "source_nrt_only",
        "Archived NetCDF is still NRT; no final published",
    )


def test_unreadable_or_unknown_title():
    published = value(at(11, hour=9))
    assert classify(DAY, at(15), at(11), published, value(None)) == (
        "title_unknown",
        "Couldn't read NetCDF title",
    )
    assert classify(DAY, at(15), at(11), published, value("Something else")) == (
        "title_unknown",
        "Unrecognised NetCDF title",
    )


def test_cog_without_cmr_record():
    status = classify(DAY, at(12), None, not_needed, not_needed)
    assert status[0] == "no_cmr_record"


def test_no_cog_is_missing():
    assert classify(DAY, None, at(19), not_needed, not_needed)[0] == "missing"
    assert classify(DAY, None, None, not_needed, not_needed)[0] == "no_cmr_granule"


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
