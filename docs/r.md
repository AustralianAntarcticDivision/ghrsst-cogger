---
title: R
nav_order: 5
---

# R
{: .no_toc }

Every R package that reads rasters through GDAL can read these COGs from a
`/vsicurl/` URL, fetching only the tiles it needs. See [The data](data.md) for the grid,
units and status fields.

1. TOC
{:toc}

## A URL for a date

```r
mur_url <- function(date, var = "analysed_sst") {
  date <- as.Date(date)
  sprintf(
    "/vsicurl/https://data.source.coop/ausantarctic/ghrsst-mur-v2/%s/%s090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_%s.tif",
    format(date, "%Y/%m/%d"), format(date, "%Y%m%d"), var
  )
}
dsn <- mur_url("2025-06-01")
```

Let GDAL skip listing the remote folder when it opens a file (set this before reading):

```r
terra::setGDALconfig("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
```

## terra

[terra](https://rspatial.github.io/terra/) applies the COG's scale and offset, so
`analysed_sst` comes back in degrees Celsius:

```r
library(terra)
sst <- rast(dsn)
sst

# A region: only the tiles that cover it are read
crop(sst, ext(130, 150, -55, -42))

# A coarse whole-world view: read from an overview
plot(sst, maxcell = 1e6)
```

Reprojecting with `by_util = TRUE` uses GDAL's warper directly, which reads the
overview that matches the output resolution, instead of the full grid:

```r
target <- rast(
  ext(c(-1, 1, -1, 1) * 6e5), res = 1000,
  crs = "+proj=laea +lon_0=147 +lat_0=-45"
)
project(sst, target, by_util = TRUE)
```

## gdalraster

[gdalraster](https://firelab.github.io/gdalraster/) exposes GDAL's API closely:

```r
library(gdalraster)
ds <- new(GDALRaster, dsn, TRUE)
ds$info()
ds$getScale(1); ds$getOffset(1)   # 0.001 and 25: degrees Celsius
ds$close()

# Warp a region to a local projection, written to a file
warp(dsn, tf <- tempfile(fileext = ".tif"),
     t_srs = "+proj=laea +lon_0=147 +lat_0=-45",
     cl_arg = c("-te", -6e5, -6e5, 6e5, 6e5, "-tr", 1000, 1000, "-r", "bilinear"))
```

## vapour

[vapour](https://hypertidy.github.io/vapour/) reads straight to R vectors through the
warper, for any extent, size and CRS in one call. Give one dimension and 0 for the
other so GDAL derives it from the extent:

```r
library(vapour)
v <- gdal_raster_data(
  dsn,
  target_ext = c(-6e5, 6e5, -6e5, 6e5),
  target_dim = c(1024, 0),
  target_crs = "+proj=laea +lon_0=147 +lat_0=-45",
  resample = "bilinear"
)
str(v)
```

The values come back as stored; apply the scale and offset shown by
`gdal raster info` (see [Values and units](data.md#values-and-units)).

## stars

```r
library(stars)
sstars <- read_stars(dsn, proxy = TRUE)
sstars[st_bbox(c(xmin = 130, xmax = 150, ymin = -55, ymax = -42), crs = st_crs(4326))]
```

## The index

The STAC GeoParquet index lists every day, its status and its asset URLs. With
[arrow](https://arrow.apache.org/docs/r/):

```r
library(arrow)
library(dplyr)

catalog <- read_parquet(
  "https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.parquet",
  col_select = c("datetime", "ghrsst:status", "assets")
)
count(catalog, `ghrsst:status`)

final <- catalog |>
  filter(`ghrsst:status` == "final") |>
  mutate(href = assets$analysed_sst$href)
```

Or with DuckDB, from R:

```r
library(duckdb)
con <- dbConnect(duckdb())
dbGetQuery(con, "
  SELECT datetime, assets.analysed_sst.href AS href
  FROM 'https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.parquet'
  WHERE \"ghrsst:status\" = 'final' AND datetime >= '2024-01-01'
  ORDER BY datetime")
```

A single day's STAC item is plain JSON:

```r
item <- jsonlite::fromJSON(
  "https://data.source.coop/ausantarctic/ghrsst-mur-v2/2025/06/01/20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.stac-item.json"
)
item$properties$`ghrsst:status`
item$assets$analysed_sst$href
```

## Many days

terra opens many COGs as the layers of one `SpatRaster` without reading pixels, then
reads only the tiles each operation touches. A year of SST at a point, from final days:

```r
dates <- seq(as.Date("2024-01-01"), as.Date("2024-12-31"), by = "1 day")
cube <- rast(mur_url(dates))
time(cube) <- dates

series <- extract(cube, cbind(147, -45), raw = TRUE)
```

Opening each file reads its header, so a year is a few hundred small requests before
any pixels. Filter `dates` to final days from the index first if you need only finals.

## raadtools

At the Australian Antarctic Division, `raadfiles::ghrsst_daily_files()` lists these same
COG URLs, and `raadtools::readghrsst()` reads them. See
[Background](background.md#what-changed-for-raadtools-users).

## Virtual stores

A GDAL multidimensional VRT over the daily COGs is coming, which opens the archive as
one time series array: `terra::rast(x, md = TRUE)`, or gdalraster's multidimensional
functions, with no per-file step. See [Virtual stores](virtual.md).
