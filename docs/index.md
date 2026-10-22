---
title: Home
nav_order: 1
---

# GHRSST MUR sea surface temperature as COGs

Every day since 2002-06-01 of the GHRSST Level 4 MUR global foundation sea surface
temperature analysis (v4.1), repackaged as Cloud Optimised GeoTIFFs (COGs) with a STAC
item per day and a STAC GeoParquet index of all days. The data are public on
[Source Cooperative](https://source.coop/ausantarctic/ghrsst-mur-v2), need no login, and
are kept up to date by a daily job.

The values are the source values. Each NetCDF variable becomes one COG per day, stored
as the same integers with the same scale and offset, on an exact north-up 0.01 degree
grid. What changes is the packaging: any tool that reads a GeoTIFF over HTTP can now read
just the part of the world, and the resolution, it needs.

## Start here

| I want to | Go to |
|---|---|
| Understand why this exists and how it replaced a local file cache | [Background](background.md) |
| Know the file layout, grid, variables, units and STAC fields | [The data](data.md) |
| Load it in Python (odc-stac, rasterio, rustac, DuckDB) | [Python](python.md) |
| Load it in R (terra, gdalraster, vapour, stars, arrow) | [R](r.md) |
| Use GDAL on the command line, DuckDB, QGIS or a browser | [Other tools](other-tools.md) |
| Treat the daily COGs as one cube, now and with the indexes that are coming | [Virtual stores](virtual.md) |
| Know how the files are made and kept final | [How it's made](pipeline.md) |

## In one minute

The `analysed_sst` COG for any day follows a template:

```
https://data.source.coop/ausantarctic/ghrsst-mur-v2/{YYYY}/{MM}/{DD}/{YYYYMMDD}090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_analysed_sst.tif
```

With GDAL (any language that wraps it):

```bash
gdalinfo /vsicurl/https://data.source.coop/ausantarctic/ghrsst-mur-v2/2025/06/01/20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_analysed_sst.tif
```

The index of every day, with each day's status (`final`, `preliminary` or `nrt_only`):

```
https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.parquet
```

Or look at a day in the browser, in any projection, with
[ortho-cog-viewer](https://mdsumner.github.io/ortho-cog-viewer/?mode=centred&proj=ortho&center=147,-42&url=preset:mur-sst-20260829).

## Preliminary and final

MUR publishes each day twice: a near-real-time (NRT) version about a day after the
date, then a final version, usually about 9 days after, that replaces it. New days here
are converted from the NRT version and re-converted when the final lands. Every STAC item
says which it is, in `ghrsst:status`. For research that needs the final analysis, filter
on `ghrsst:status = 'final'`. See [The data](data.md#preliminary-and-final).

## Coming

Virtual indexes over the same COGs, so the whole archive opens as one array without
copying any data: a GDAL multidimensional VRT, a flat tabular index of every tile, a
kerchunk Parquet reference store and an Icechunk store. See [Virtual stores](virtual.md).

## Source and citation

The source is the
[GHRSST Level 4 MUR Global Foundation Sea Surface Temperature Analysis (v4.1)](https://podaac.jpl.nasa.gov/dataset/MUR-JPL-L4-GLOB-v4.1)
from NASA JPL, distributed by PO.DAAC, [doi:10.5067/GHGMR-4FJ04](https://doi.org/10.5067/GHGMR-4FJ04).
Please cite the source dataset as PO.DAAC asks in
[Citing PO.DAAC](https://podaac.jpl.nasa.gov/CitingPODAAC). Data hosted by PO.DAAC is
openly shared, without restriction, in accordance with NASA's Earth Science program Data
and Information Policy.
