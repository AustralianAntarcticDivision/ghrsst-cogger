import xml.etree.ElementTree as ET

import pytest

from ghrsst.create_vrt import PARQUET_URL, publish_vrt, read_parquet_items

HREF = "https://data.source.coop/ausantarctic/ghrsst-mur-v2/{d:%Y/%m/%d}/{d:%Y%m%d}090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_{a}.tif"

BANDS = {
    "analysed_sst": {
        "data_type": "int16",
        "nodata": -32768,
        "scale": 0.001,
        "offset": 25.0,
        "unit": "celsius",
    },
    "sst_anomaly": {
        "data_type": "int16",
        "nodata": -32768,
        "scale": 0.001,
        "offset": 0.0,
        "unit": "celsius",
    },
    "mask": {"data_type": "int8", "nodata": -128},
}


def child(el: ET.Element, tag: str) -> ET.Element:
    found = el.find(tag)
    assert found is not None, f"<{tag}> missing under <{el.tag}>"
    return found


def text(el: ET.Element, tag: str) -> str:
    return child(el, tag).text or ""


def attr(el: ET.Element, name: str) -> str:
    value = el.get(name)
    assert value is not None, f"{name}= missing on <{el.tag}>"
    return value


def group_of(path: str) -> ET.Element:
    return child(ET.parse(path).getroot(), "Group")


def dims_of(group: ET.Element) -> dict[str, int]:
    return {attr(d, "name"): int(attr(d, "size")) for d in group.findall("Dimension")}


def make_items(n=3, anomaly_from=1):
    """STAC items shaped like the real ones (as rustac reads them)."""
    from datetime import date, timedelta

    items = []
    for i in range(n):
        d = date(2013, 12, 31) + timedelta(days=i)
        assets = {
            a: {"href": HREF.format(d=d, a=a), "roles": ["data"], "raster:bands": [b]}
            for a, b in BANDS.items()
            if a != "sst_anomaly" or i >= anomaly_from
        }
        items.append(
            {
                "type": "Feature",
                "id": f"{d:%Y%m%d}090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.stac-item",
                "properties": {
                    "datetime": f"{d:%Y-%m-%d}T00:00:00Z",
                    "proj:epsg": 4326,
                    "proj:shape": [17999, 36000],
                    "proj:transform": [
                        0.01,
                        0.0,
                        -179.995,
                        0.0,
                        -0.01,
                        89.995,
                        0,
                        0,
                        1,
                    ],
                },
                "assets": assets,
            }
        )
    return items


def test_vrt_structure(tmp_path):
    out = publish_vrt(make_items(), str(tmp_path))
    group = group_of(out)
    assert dims_of(group) == {"time": 3, "y": 17999, "x": 36000}

    arrays = {attr(a, "name"): a for a in group.findall("Array")}
    assert set(arrays) == {"time", "y", "x", *BANDS}

    sst = arrays["analysed_sst"]
    assert text(sst, "DataType") == "Int16"
    assert float(text(sst, "Scale")) == 0.001
    assert float(text(sst, "Offset")) == 25.0
    assert text(sst, "Unit") == "celsius"
    assert text(sst, "BlockSize") == "1,512,512"
    assert attr(child(sst, "SRS"), "dataAxisToSRSAxisMapping") == "2,3"
    assert len(sst.findall("Source")) == 3

    # sst_anomaly missing on the first day: two sources, placed at days 1 and 2
    anom = arrays["sst_anomaly"].findall("Source")
    assert [attr(child(s, "DestSlab"), "offset") for s in anom] == ["1,0,0", "2,0,0"]
    assert text(anom[0], "SourceFilename").startswith(
        "/vsicurl/https://data.source.coop/"
    )

    # one day apart, from 2013-12-31
    t = child(arrays["time"], "RegularlySpacedValues")
    assert float(attr(t, "increment")) == 1.0
    assert text(arrays["time"], "Unit") == "days since 1970-01-01"


@pytest.mark.network
def test_vrt_from_published_parquet(tmp_path):
    import asyncio

    items = asyncio.run(read_parquet_items(PARQUET_URL))
    out = publish_vrt(items, str(tmp_path))
    group = group_of(out)
    assert dims_of(group)["time"] == len(items)
    assert {attr(a, "name") for a in group.findall("Array")} >= {"analysed_sst", "mask"}
