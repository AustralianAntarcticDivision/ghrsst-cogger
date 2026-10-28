---
title: The data
nav_order: 3
---

# The data
{: .no_toc }

1. TOC
{:toc}

## Where it is

| | |
|---|---|
| Landing page | <https://source.coop/ausantarctic/ghrsst-mur-v2> |
| HTTPS root | `https://data.source.coop/ausantarctic/ghrsst-mur-v2/` |
| S3 (anonymous, `us-west-2`) | `s3://us-west-2.opendata.source.coop/ausantarctic/ghrsst-mur-v2/` |
| STAC GeoParquet index | `https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.parquet` |
| Dates | 2002-06-01 to about two days ago, daily |

HTTPS works from anywhere. The S3 path is the same bucket, read without credentials,
which is the faster route for compute already in AWS `us-west-2`:

```bash
aws s3 ls --no-sign-request s3://us-west-2.opendata.source.coop/ausantarctic/ghrsst-mur-v2/2025/06/01/
```

With GDAL, either `/vsicurl/https://data.source.coop/...` or, with
`AWS_NO_SIGN_REQUEST=YES` and `AWS_REGION=us-west-2`,
`/vsis3/us-west-2.opendata.source.coop/ausantarctic/ghrsst-mur-v2/...`.

## Layout and names

Each day has a folder `{YYYY}/{MM}/{DD}/`, and its files keep the source NetCDF's name,
with the variable appended:

```
2025/06/01/
  20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.stac-item.json
  20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_analysed_sst.tif
  20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_analysis_error.tif
  20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_mask.tif
  20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_sea_ice_fraction.tif
  20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_sst_anomaly.tif
```

So a URL can be built from a date without a listing. The index is still the way to know
which dates exist and what state they're in (see [The index](#the-index)).

## Variables

| Variable | What it is |
|---|---|
| `analysed_sst` | Foundation sea surface temperature, the analysis itself |
| `analysis_error` | Estimated error standard deviation of `analysed_sst` |
| `mask` | Surface type flags (open sea, land, lake, ice). Integer flags, not a measurement |
| `sea_ice_fraction` | Sea ice area fraction |
| `sst_anomaly` | Difference from a reference climatology. Only in source files that have it, so older days have four COGs, not five |

The source's `dt_1km_data` variable (time to the nearest 1 km observation) isn't
converted. For the full definitions see the
[MUR product page](https://podaac.jpl.nasa.gov/dataset/MUR-JPL-L4-GLOB-v4.1).

## Grid

Every COG is on the same grid:

| | |
|---|---|
| CRS | `EPSG:4326` (longitude, latitude) |
| Size | 36000 columns by 17999 rows |
| Pixel size | 0.01 degree |
| Geotransform | `-179.995, 0.01, 0, 89.995, 0, -0.01` (GDAL order) |
| Extent | longitude -179.995 to 180.005, latitude -89.995 to 89.995 |
| Orientation | North-up (the source NetCDF is stored south to north) |

Inside each COG:

| | |
|---|---|
| Tiles | 512 by 512 pixels (71 by 36 tiles at full resolution) |
| Compression | ZSTD, with horizontal differencing (TIFF predictor 2) |
| Overviews | 5 levels, at 2, 4, 8, 16 and 32 times coarser (0.32 degree at the coarsest), made by nearest neighbour, so each overview pixel is a source pixel, not an average |
| Size | About 250 MB for one day's `analysed_sst` |

Pixel centres are the source's coordinates exactly: longitude -179.99 to 180.00 and
latitude -89.99 to 89.99, in 0.01 steps. Note the grid runs 0.005 degree past the
antimeridian on the east side, because the source's first column is centred at
-179.99, not -179.995.

## Values and units

Values are stored as the source's integers, with nodata and a scale and offset to
convert them. For `analysed_sst` the stored type is `int16` with nodata `-32768` and a
scale of `0.001`.

The COG's own metadata (what GDAL and tools built on it, such as terra, rasterio and
QGIS, apply) and the STAC item's `raster:bands` agree: `analysed_sst`, `analysis_error`
and `sst_anomaly` give **degrees Celsius**. For `analysed_sst` that's offset `25`, where
the source NetCDF has the same integers with offset `298.15` in Kelvin.

To see the COG's metadata:

```bash
gdalinfo /vsicurl/https://data.source.coop/ausantarctic/ghrsst-mur-v2/2025/06/01/20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_analysed_sst.tif | grep -E "Offset|Scale|Unit|NoData"
```

Tools that return the raw integers (for example `gdallocationinfo`, or a warp to an
integer type) need the scale and offset applied by hand.

## STAC items

Each day has a STAC item (STAC 1.1) with one asset per COG. Beyond the core fields it
uses the [Projection](https://github.com/stac-extensions/projection),
[Raster](https://github.com/stac-extensions/raster),
[Processing](https://github.com/stac-extensions/processing) and
[File](https://github.com/stac-extensions/file) extensions:

| Field | Where | Meaning |
|---|---|---|
| `start_datetime`, `end_datetime`, `datetime` | properties | The day, 00:00:00 to 23:59:59 UTC |
| `proj:*` | properties | CRS, shape, transform and bbox of the grid above |
| `ghrsst:status` | properties | `final`, `preliminary` or `nrt_only` (below) |
| `processing:lineage` | properties | The source NetCDF's title, e.g. `Daily MUR SST, Final product` |
| `processing:datetime` | properties | When JPL produced the source NetCDF |
| `ghrsst:source_published` | properties | When PO.DAAC published the source NetCDF |
| `processing:version`, `processing:level`, `processing:facility` | properties | `04.1`, `L4`, `NASA JPL` |
| `processing:software` | properties | Software versions that made the COGs (set when they're made) |
| `updated` | properties | When the COGs were last made |
| `raster:bands` | each asset | Data type, nodata, scale, offset and unit (Celsius for the temperatures) |
| `file:checksum`, `file:size` | each asset | SHA2-256 multihash and size of the COG, when it was made |
| `derived_from` link | links | The source NetCDF at PO.DAAC (needs an Earthdata login) |

## Preliminary and final

MUR publishes each day twice, replacing the file in place under the same name:

- **Near-real-time (NRT):** about 1 to 2 days after the date. Its title says
  "Interim near-real-time (nrt) product".
- **Final:** later, usually about 9 days after the date (from 2020 onwards; about 4 days
  in 2016, and occasionally up to about 40 days). Its title says "Final product".

New days here are converted from the NRT version as soon as it's published, then
re-converted when the final appears. `ghrsst:status` says which one a day's COGs came
from:

| `ghrsst:status` | Meaning |
|---|---|
| `final` | Made from the final version. |
| `preliminary` | Made from the NRT version. A final is expected, and the COGs will be replaced. |
| `nrt_only` | The archived source is still the NRT version more than 50 days after the date, so no final was published. Re-converting it would change nothing. |

For research that needs the final analysis, use only `final` days. For monitoring the
last week or so, `preliminary` days are the most recent data there is.

### Replaced files keep their URLs

When a final replaces a preliminary day, its COGs are rewritten at the same URLs, so
their contents and the byte offsets of their tiles change. If you cache files, or hold
byte-range references into them (kerchunk, virtual Zarr, Icechunk virtual chunks),
compare `file:checksum` (or the HTTP `ETag`) and refresh when it changes. `final` days
don't change.

## The index

`ghrsst-mur-v2.parquet` is a [STAC GeoParquet](https://github.com/stac-utils/stac-geoparquet)
file with one row per day, rebuilt every day at 01:00 UTC. Item properties are columns
(`datetime`, `ghrsst:status`, `processing:lineage`, ...), and `assets` is a struct column
holding each COG's `href` and its fields. It's small enough to read whole, and any
Parquet reader can filter it: see [Python](python.md#the-index),
[R](r.md#the-index) and [DuckDB](other-tools.md#duckdb).

## Licence and citation

The data are NASA JPL's GHRSST MUR v4.1, distributed by PO.DAAC,
[doi:10.5067/GHGMR-4FJ04](https://doi.org/10.5067/GHGMR-4FJ04), and are openly shared
without restriction under NASA's Earth Science program Data and Information Policy. Cite
the source dataset as described in [Citing PO.DAAC](https://podaac.jpl.nasa.gov/CitingPODAAC).
