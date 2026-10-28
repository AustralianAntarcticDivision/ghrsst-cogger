---
title: Python
nav_order: 4
---

# Python
{: .no_toc }

Ways to read the COGs from Python, from one day's STAC item to a lazy cube of many
days. See [The data](data.md) for the grid, units and status fields these rely on.

1. TOC
{:toc}

## One day, via its STAC item

[odc-stac](https://github.com/opendatacube/odc-stac) loads STAC items into xarray, lazily
with dask when `chunks` is given:

```python
from odc.stac import load
from pystac import Item

base = "https://data.source.coop/ausantarctic/ghrsst-mur-v2"
item = Item.from_file(
    f"{base}/2025/06/01/20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.stac-item.json"
)
print(item.properties["ghrsst:status"])  # final, preliminary or nrt_only

data = load([item], bands=["analysed_sst"], chunks={}, anchor="center")
```

odc-stac returns the stored integers. Apply the scale and offset from the item's
`raster:bands`, which give degrees Celsius:

```python
band = item.assets["analysed_sst"].extra_fields["raster:bands"][0]
sst = data["analysed_sst"].squeeze()
sst_c = sst.where(sst != band["nodata"]) * band["scale"] + band["offset"]

sst_c.sel(longitude=slice(130, 150), latitude=slice(-42, -55)).plot.imshow(
    robust=True, cmap="inferno"
)
```

`anchor="center"` keeps odc-stac on the files' own pixel grid, so nothing is resampled.

## One day, one region, straight from the COG

Without STAC, [rasterio](https://rasterio.readthedocs.io) reads a window of the COG
directly, fetching only the tiles that cover it:

```python
import rasterio
from rasterio.windows import from_bounds

url = (
    "https://data.source.coop/ausantarctic/ghrsst-mur-v2/2025/06/01/"
    "20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_analysed_sst.tif"
)

with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
    with rasterio.open(url) as src:
        window = from_bounds(130, -55, 150, -42, src.transform)
        raw = src.read(1, window=window, masked=True)
        # The COG's own scale and offset give degrees Celsius
        sst_c = raw * src.scales[0] + src.offsets[0]
```

For a coarse whole-world view, ask for a smaller output and GDAL reads an overview
instead of the full grid:

```python
with rasterio.open(url) as src:
    small = src.read(1, out_shape=(900, 1800), masked=True) * src.scales[0] + src.offsets[0]
```

[rioxarray](https://corteva.github.io/rioxarray/) does the same with xarray:
`rioxarray.open_rasterio(url, chunks=True, mask_and_scale=True)`.

## The index

The STAC GeoParquet index has one row per day. [rustac](https://github.com/stac-utils/rustac-py)
reads it into STAC items (its functions are async, so use `await` in a notebook, or
`asyncio.run` in a script):

```python
import asyncio
import pystac
import rustac

url = "https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.parquet"

features = asyncio.run(
    rustac.search(url, datetime="2024-01-01T00:00:00Z/2024-12-31T23:59:59Z")
)
# Keep only days made from the final (not near-real-time) version
items = [
    pystac.Item.from_dict(f)
    for f in features
    if f["properties"].get("ghrsst:status") == "final"
]
```

Or treat it as a table. With [DuckDB](https://duckdb.org):

```python
import duckdb

url = "https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.parquet"
duckdb.sql(f"""
    SELECT "ghrsst:status" AS status, count(*) AS days,
           min(datetime) AS first, max(datetime) AS last
    FROM '{url}'
    GROUP BY ALL ORDER BY first
""").show()

hrefs = duckdb.sql(f"""
    SELECT datetime, assets.analysed_sst.href AS href
    FROM '{url}'
    WHERE "ghrsst:status" = 'final' AND datetime >= '2024-01-01'
    ORDER BY datetime
""").df()
```

pandas or geopandas (`geopandas.read_parquet`) read it too.

## Many days as a lazy cube

Pass a list of items to odc-stac with a small `bbox`, and the result is a dask-backed
cube that reads only the tiles each computation needs. A year of daily SST at one
location, from final days only:

```python
from odc.stac import load

# items: final days in 2024, from the index above
cube = load(
    items,
    bands=["analysed_sst"],
    bbox=(146.5, -45.5, 147.5, -44.5),
    chunks={},
    anchor="center",
)
band = items[0].assets["analysed_sst"].extra_fields["raster:bands"][0]
sst = cube["analysed_sst"]
sst_c = sst.where(sst != band["nodata"]) * band["scale"] + band["offset"]

series = sst_c.sel(longitude=147.0, latitude=-45.0, method="nearest").compute()
```

[stackstac](https://github.com/gjoseph92/stackstac) builds the same kind of cube from the
same items.

## Virtual stores

A GDAL multidimensional VRT over the COGs, [`ghrsst-mur-v2.vrt`](https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.vrt), opens the
whole archive as one array per variable with GDAL's bindings
(`gdal.OF_MULTIDIM_RASTER`), with no STAC step. See
[Virtual stores](virtual.md#gdal-multidimensional-vrt) for an example. Kerchunk Parquet
references and an Icechunk store are coming, which will open the archive as one xarray dataset
without GDAL.

## Tips

- Set `GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR` (as above) so GDAL doesn't list the
  remote folder when it opens a file.
- In AWS `us-west-2`, read through S3 instead of HTTPS:
  `s3://us-west-2.opendata.source.coop/ausantarctic/ghrsst-mur-v2/...` with anonymous
  access (`AWS_NO_SIGN_REQUEST=YES` for GDAL and rasterio).
- A whole day of `analysed_sst` at full resolution is 36000 by 17999 pixels, about
  1.3 GB as `int16` in memory. Subset first, or read an overview.
