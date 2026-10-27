---
title: Background
nav_order: 2
---

# Background: from a local file cache to a public cloud copy
{: .no_toc }

1. TOC
{:toc}

## The local cache

For many years the Australian Antarctic Division has kept a shared collection of the
environmental data its science uses most: sea ice, sea surface temperature, ocean colour,
altimetry, winds, ocean model output, bathymetry. The collection lives on a shared file
system, is kept current by scheduled mirrors of each upstream archive (tools like
[bowerbird](https://github.com/ropensci/bowerbird)), and is reached from R through two
packages:

- [raadfiles](https://github.com/AustralianAntarcticDivision/raadfiles) indexes the
  files: for each dataset, a table of file paths and dates.
- [raadtools](https://github.com/AustralianAntarcticDivision/raadtools) reads them:
  `readghrsst(date, xylim = ...)` returns a raster for a date and a region.

GHRSST MUR was one of those mirrors: one NetCDF file per day from 2002-06-01, about
8,900 files and several terabytes, with up to six gridded variables in each.

This works well for anyone on the network with the file system mounted. It has costs
that grow with time:

- **Access is local.** The files are only fast, or only there at all, for people and
  machines on that network. Collaborators, cloud compute and the public can't use the
  same copy.
- **Every byte is mirrored.** To read a small region for a few days, the whole daily
  files have to be fetched and stored first.
- **The upstream needs a login.** PO.DAAC's archive requires an Earthdata login for each
  file, so the mirror carries credentials and anyone else needs their own.
- **Each reader fixes the grid again.** The NetCDF stores latitude south to north, and its
  coordinates are pixel centres with no explicit grid registration. Every tool that
  turns a file into a georeferenced raster has to get the flip and the half-pixel origin
  right, and they haven't always agreed.
- **The source changes in place.** Each day's file is replaced, under the same name, when
  the final version supersedes the near-real-time one. A mirror that copies a file once
  keeps whichever version it saw first.

## The cloud copy

This repository makes a different kind of copy. Instead of mirroring the NetCDF files,
it converts each day once, in the cloud, into a form that anyone can read in part over
HTTP, and publishes it openly on
[Source Cooperative](https://source.coop/ausantarctic/ghrsst-mur-v2):

- **One COG per variable per day.** Cloud Optimised GeoTIFFs are tiled and carry
  overviews, so a reader fetches only the tiles of the region and resolution it needs,
  with HTTP range requests. A whole-world preview reads a few small overview tiles,
  not the full 36000 by 17999 grid.
- **The values are unchanged.** The integers are written as they are in the NetCDF,
  with a scale and offset to convert them, so nothing is lost or rounded. (For
  `analysed_sst` the COG's offset gives degrees Celsius; the STAC item keeps the
  source's Kelvin.)
- **The grid is fixed once.** North-up, with an exact geotransform (0.01 degree pixels,
  top-left corner at -179.995, 89.995), so every GDAL-based reader agrees on where each
  pixel is.
- **STAC metadata for every day,** and a STAC GeoParquet file indexing every day, so a
  date range or a status can be found with one small read instead of a file listing.
- **No login, no mirror.** The files are public, and the copy that's read is the one
  that's maintained.
- **Final, and says so.** Each day is re-converted when its final version is published,
  and its STAC item records which version it came from (see
  [Preliminary and final](data.md#preliminary-and-final)).

## What changed for raadtools users

Nothing in the reading code. The catalogue layer decides where the bytes come from, and
the readers take whatever it gives them. `raadfiles::ghrsst_daily_files()` now returns a
`/vsicurl/` URL to each day's `analysed_sst` COG on Source Cooperative instead of a local
NetCDF path, and `raadtools::readghrsst()` reads those through GDAL as before. The local
NetCDF files are still listed by `raadfiles::ghrsst_daily_files_netcdf()`.

```r
files <- raadfiles::ghrsst_daily_files()
tail(files$fullname, 1)
#> "/vsicurl/https://data.source.coop/ausantarctic/ghrsst-mur-v2/.../..._analysed_sst.tif"

sst <- raadtools::readghrsst("2025-06-01", xylim = terra::ext(130, 150, -55, -42))
```

The same `/vsicurl/` URLs work from anywhere, with or without raadtools.

## Preliminary data, and the audit

The bulk conversions in early 2025 used final files for nearly every date, but the
finals for the last days of that run, after 2025-02-01, weren't published yet, so those
were made from near-real-time files. From 2025-02-10 a daily job converted each new day
about two days after its date, which is again the near-real-time version, and it never
revisited a day once converted. So from 2025-02-02 the COGs were preliminary, although
nothing said so.

In October 2026 an audit compared every COG's write time with when each source file was
produced and published, and read each source file's own title. It found:

| Result | Days |
|---|---|
| Made from the final version | 8,230 (of the 8,282 days from 2002-06-01 to 2025-02-01) |
| Made from a near-real-time version since replaced by a final | 623 (2025-02-02 onwards) |
| Made from a near-real-time version whose final was not yet out | 1 |
| Source still near-real-time years later (no final was ever published) | 50 |
| Not yet published, or a temporary gap in the catalogue | 4 |

The 623 preliminary days were re-converted from their finals, every STAC item now
records its status and provenance, and the daily job now revisits recent days until
each one is final. Re-converted COGs keep their URLs, so they record a checksum and an
`updated` time for anyone who holds byte references into them. The method is in
[How it's made](pipeline.md#preliminary-and-final-in-the-pipeline).

## Where it's going

The COGs are the storage. The next layer is indexes that let the archive be opened as a
single time series array in the tools people already use, without a second copy of the
data: a GDAL multidimensional VRT, a flat tabular index of every tile, kerchunk Parquet
references and an Icechunk store. See [Virtual stores](virtual.md).

The same approach, a public cloud copy with a catalogue and virtual indexes, suits
other datasets from the local collection, and is planned for more of them.
