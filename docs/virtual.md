---
title: Virtual stores
nav_order: 7
---

# Virtual stores: the archive as one array
{: .no_toc }

The COGs are the storage. A virtual store is an index over them that lets the whole
archive be opened as a single array (time, latitude, longitude) for each variable,
without copying or converting any data. This page covers how that works, what you can
do today, and the indexes that are coming.

1. TOC
{:toc}

## A COG is already chunked

A tiled GeoTIFF is a chunked array with its chunk manifest in the header. Each day's
`analysed_sst` COG is a 17999 by 36000 array cut into 512 by 512 tiles; its header lists
the byte offset and length of every tile, then the same again for each overview level.
A reader that has the header can fetch any tile with one HTTP range request.

You can see this for one file on the
[playproj tiles and overviews page](https://hypertidy.github.io/playproj/structure.html#src=https%3A%2F%2Fdata.source.coop%2Fausantarctic%2Fghrsst-mur-v2%2F2025%2F06%2F01%2F20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_analysed_sst.tif&kind=tiff):
it reads only the header, draws the tiles and overview levels, and gives the byte range
of any tile you click.

So the archive is already a chunked cube: about 8,900 days, each 36 by 71 tiles per
variable, plus overviews. What's missing is a single description of the cube: the time
axis, which file holds which day, and where each tile is, in a form that array tools can
open directly.

## What you can do today

Build the cube from the catalogue at read time. Each tool reads the STAC index, opens
each day's COG header, and assembles a lazy array:

| Tool | How |
|---|---|
| Python, [odc-stac](https://github.com/opendatacube/odc-stac) | `odc.stac.load(items, chunks={})` gives a dask-backed xarray cube ([example](python.md#many-days-as-a-lazy-cube)) |
| Python, [stackstac](https://github.com/gjoseph92/stackstac) | `stackstac.stack(items)`, the same idea |
| R, [terra](https://rspatial.github.io/terra/) | `rast(urls)` with `time()` set, one layer per day ([example](r.md#many-days)) |
| GDAL | `gdal raster stack` over a list of URLs, one band per day ([example](other-tools.md#gdal-command-line)) |

These work now and need nothing new. Their cost is at the start: every session reads
the index and then the header of every day it uses, a few hundred requests for a year,
before any pixels.

## Coming

Four indexes over the same COGs, published alongside them on Source Cooperative. Each
one describes the archive once, so opening it is a single small read.

### GDAL multidimensional VRT

One VRT file that declares the dimensions (time, latitude, longitude) and an array per
variable, with each time step pointing at that day's COG and the scale, offset and
nodata declared once. GDAL opens it as a multidimensional dataset, so anything built on
GDAL's multidimensional API can slice it by time and space:

- GDAL: `gdal mdim info` and `gdal mdim convert`
- R: `terra::rast(x, md = TRUE)`, or [gdalraster](https://firelab.github.io/gdalraster/)'s multidimensional functions
- Python: `osgeo.gdal` with `gdal.OF_MULTIDIM_RASTER`, or xarray through a GDAL backend
  such as [gdalxarray](https://github.com/hypertidy/gdalxarray)

GDAL reads the tiles from the COGs as it would for any GeoTIFF, so nothing about the
codecs or the tile layout needs to be described.

### A flat tabular index

One row per tile: date, variable, overview level, tile column and row, the COG's URL,
the tile's byte offset and length, and the COG's checksum. This is the next level down
from the STAC GeoParquet, which has one row per day. As a Parquet table it can be
queried with DuckDB, arrow or Polars to plan exactly which byte ranges a region and
date range need, and the other indexes can be generated from it.

### kerchunk Parquet references

A [kerchunk](https://github.com/fsspec/kerchunk) reference store in Parquet: a Zarr
(version 2) view of the archive in which each chunk is a reference to a tile's byte
range in a COG. It opens with fsspec's reference file system and xarray's Zarr engine,
in any environment that has them, with no GDAL.

### Icechunk

An [Icechunk](https://icechunk.io) store whose chunks are virtual references into the
COGs: a Zarr (version 3) view, with Icechunk's transactions and version history. When
a preliminary day is replaced by its final, the update is a new commit, so earlier
analyses can still name the exact version they used.

## Things the indexes have to get right

- **Codecs.** The tiles are ZSTD-compressed with horizontal differencing (TIFF predictor
  2). GDAL handles that natively. Zarr-based stores have to declare matching codecs for
  every chunk.
- **Edges.** The grid is 17999 by 36000, which isn't a whole number of 512-pixel tiles.
  TIFF stores the edge tiles at full size, padded, which is what a regular Zarr chunk
  grid expects.
- **Overviews.** Each overview level is its own array, with its own tiles: a set of
  multiscales rather than one array.
- **Days without `sst_anomaly`.** Older source files don't have it, so that array has
  missing time steps.
- **Change.** Preliminary days are rewritten at the same URLs when their final arrives,
  so references to them go stale. The indexes will record each COG's `file:checksum`
  and be rebuilt as days are finalised. `final` days don't change.

## Why not convert to Zarr?

A Zarr copy would duplicate several terabytes that are already chunked, compressed and
public, and it would need its own process to keep it in step as days are finalised.
The virtual stores give array tools the same view with only an index to maintain, and
GDAL-based tools keep reading the COGs directly.
