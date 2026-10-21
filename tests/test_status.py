from datetime import UTC, date, datetime

import pytest

from ghrsst.cogger import (
    PROCESSING_EXTENSION,
    REASON_FINAL_SOURCE,
    REASON_NO_CMR,
    REASON_NO_COG,
    REASON_NO_DATA,
    REASON_NRT_ONLY,
    REASON_NRT_SOURCE,
    REASON_PUBLISHED_AFTER,
    REASON_PUBLISHED_UNKNOWN,
    REASON_REGENERATED,
    REASON_TITLE_UNKNOWN,
    REASON_TITLE_UNREADABLE,
    REASON_TOO_CLOSE,
    STATUS_PROPERTY,
    action_for,
    apply_status,
    set_stac_status,
    source_provenance,
    status_from_title,
)

FINAL = "Daily MUR SST, Final product"
NRT = "Daily MUR SST, Interim near-real-time (nrt) product"
TODAY = date(2026, 10, 21)


def test_status_from_title():
    assert status_from_title(date(2026, 10, 1), FINAL, TODAY) == "final"
    assert status_from_title(date(2026, 10, 19), NRT, TODAY) == "preliminary"
    assert status_from_title(date(2015, 4, 22), NRT, TODAY) == "nrt_only"
    assert status_from_title(date(2026, 10, 1), "Something else", TODAY) is None
    assert status_from_title(date(2026, 10, 1), None, TODAY) is None


@pytest.mark.parametrize(
    ("status", "reason", "action"),
    [
        ("final", REASON_FINAL_SOURCE, "set_status"),
        ("nrt_only", REASON_NRT_ONLY, "set_status"),
        # Current NetCDF is still NRT: re-COGing would give the same data
        ("preliminary", REASON_NRT_SOURCE, "set_status"),
        # COG made from an older NetCDF than the current one
        ("preliminary", REASON_REGENERATED, "reprocess"),
        ("preliminary", REASON_PUBLISHED_AFTER, "reprocess"),
        ("too_close", REASON_TOO_CLOSE, "reprocess"),
        ("missing", REASON_NO_COG, "reprocess"),
        ("no_cmr_granule", REASON_NO_DATA, "skip"),
        ("no_cmr_record", REASON_NO_CMR, "retry"),
        ("published_unknown", REASON_PUBLISHED_UNKNOWN, "retry"),
        ("title_unknown", REASON_TITLE_UNREADABLE, "retry"),
        ("title_unknown", REASON_TITLE_UNKNOWN, "retry"),
    ],
)
def test_action_for(status, reason, action):
    assert action_for(status, reason) == action


PRODUCED = datetime(2025, 12, 19, 7, 17, 35, tzinfo=UTC)
PUBLISHED = datetime(2025, 12, 19, 7, 32, 16, tzinfo=UTC)
ATTRS = {"title": FINAL, "product_version": "04.1", "processing_level": "L4"}


def test_source_provenance_maps_to_processing_fields():
    props = source_provenance(PRODUCED, PUBLISHED, ATTRS, converting=False)
    assert props == {
        "processing:facility": "NASA JPL",
        "processing:datetime": "2025-12-19T07:17:35Z",
        "ghrsst:source_published": "2025-12-19T07:32:16Z",
        "processing:lineage": FINAL,
        "processing:version": "04.1",
        "processing:level": "L4",
    }


def test_source_provenance_software_only_when_converting():
    props = source_provenance(PRODUCED, PUBLISHED, ATTRS, converting=True)
    assert {"GDAL", "rasterio", "ghrsst-cogger"} <= set(props["processing:software"])
    # Unknown times and attributes are left out, not guessed
    assert source_provenance(None, None, None, converting=False) == {
        "processing:facility": "NASA JPL"
    }


def stac_item() -> dict:
    """A STAC item as written before this change: no status, canonical NetCDF link."""
    return {
        "type": "Feature",
        "stac_version": "1.1.0",
        "stac_extensions": [
            "https://stac-extensions.github.io/projection/v1.1.0/schema.json"
        ],
        "id": "20251210090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.stac-item",
        "properties": {
            "datetime": "2025-12-10T00:00:00Z",
            "processing:software": {"ghrsst-cogger": "0.12.2"},
        },
        "links": [
            {"rel": "self", "href": "https://example.com/item.json"},
            {
                "rel": "canonical",
                "href": "https://archive.podaac.earthdata.nasa.gov/x.nc",
                "type": "application/netcdf",
                "title": "Original NetCDF",
            },
        ],
        "assets": {},
    }


def test_apply_status():
    provenance = source_provenance(PRODUCED, PUBLISHED, ATTRS, converting=False)
    doc = apply_status(stac_item(), "final", provenance)

    assert doc["properties"][STATUS_PROPERTY] == "final"
    assert doc["properties"]["processing:lineage"] == FINAL
    assert doc["properties"]["ghrsst:source_published"] == "2025-12-19T07:32:16Z"
    # A status-only update keeps the software that made the existing COG
    assert doc["properties"]["processing:software"] == {"ghrsst-cogger": "0.12.2"}
    assert PROCESSING_EXTENSION in doc["stac_extensions"]
    rels = [(link["rel"], link.get("title")) for link in doc["links"]]
    assert ("derived_from", "Original NetCDF") in rels
    assert all(rel != "canonical" for rel, _ in rels)

    # Applying again doesn't duplicate the extension
    apply_status(doc, "final", provenance)
    assert doc["stac_extensions"].count(PROCESSING_EXTENSION) == 1


def test_set_stac_status_on_disk(tmp_path):
    import json

    stac_file = tmp_path / "item.stac-item.json"
    stac_file.write_text(json.dumps(stac_item()))
    set_stac_status(stac_file, "nrt_only", {"processing:facility": "NASA JPL"})
    doc = json.loads(stac_file.read_text())
    assert doc["properties"][STATUS_PROPERTY] == "nrt_only"
    assert doc["links"][1]["rel"] == "derived_from"


def test_dates_to_queue_skips_final():
    from datetime import datetime as dt

    from ghrsst.dategen import dates_to_queue

    dates = [dt(2026, 10, d) for d in (1, 2, 3)]
    final = {dt(2026, 10, 2)}
    assert dates_to_queue(dates, lambda d: d in final) == [
        dt(2026, 10, 1),
        dt(2026, 10, 3),
    ]
    # --include-final queues everything, without checking
    assert dates_to_queue(dates, lambda d: pytest.fail("not checked"), True) == dates


def test_oldest_write():
    from ghrsst.cogger import oldest_write

    old = datetime(2025, 12, 12, 0, 3, tzinfo=UTC)
    new = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    five = {
        "analysed_sst": new,
        "analysis_error": old,
        "mask": new,
        "sea_ice_fraction": new,
        "sst_anomaly": new,
    }
    assert oldest_write(five) == old
    # Older dates have no sst_anomaly COG at all: still a complete date
    four = {**five, "sst_anomaly": None}
    assert oldest_write(four) == old
    # No analysed_sst means the date's COGs are missing
    assert oldest_write({**five, "analysed_sst": None}) is None
    assert oldest_write({}) is None


def test_partial_re_cog_does_not_look_final():
    from ghrsst.cogger import classify, oldest_write

    # 2025-12-10: original COGs written 2025-12-12 (NRT), final produced and published
    # 2025-12-19. A re-COG on 2026-01-05 wrote analysed_sst, then stopped
    original = datetime(2025, 12, 12, 0, 3, tzinfo=UTC)
    rewritten = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    produced = datetime(2025, 12, 19, 7, 17, tzinfo=UTC)
    cogs = oldest_write(
        {
            "analysed_sst": rewritten,
            "analysis_error": original,
            "mask": original,
            "sea_ice_fraction": original,
            "sst_anomaly": original,
        }
    )

    status = classify(
        date(2025, 12, 10), cogs, produced, lambda: produced, lambda: FINAL, TODAY
    )
    assert status == ("preliminary", REASON_REGENERATED)
    assert action_for(*status) == "reprocess"
    # With only analysed_sst's time it would have wrongly been final
    assert (
        classify(
            date(2025, 12, 10),
            rewritten,
            produced,
            lambda: produced,
            lambda: FINAL,
            TODAY,
        )[0]
        == "final"
    )


def test_file_fields():
    import hashlib

    from ghrsst.cogger import file_fields

    fields = file_fields(b"cog bytes")
    assert fields["file:size"] == 9
    # SHA2-256 multihash: 0x12 (sha2-256), 0x20 (32 bytes), then the digest
    assert fields["file:checksum"] == "1220" + hashlib.sha256(b"cog bytes").hexdigest()
