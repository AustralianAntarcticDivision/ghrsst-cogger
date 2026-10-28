---
title: Other tools
nav_order: 6
---

# GDAL, DuckDB, QGIS and the browser
{: .no_toc }

1. TOC
{:toc}

## GDAL command line

The `gdal` command (GDAL 3.11 and later) reads the COGs from a `/vsicurl/` URL. Set
`GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR` so GDAL doesn't list the remote folder:

```bash
export GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR
COG=/vsicurl/https://data.source.coop/ausantarctic/ghrsst-mur-v2/2025/06/01/20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_analysed_sst.tif

# Size, grid, tiling, overviews, scale and offset
gdal raster info $COG

# The value at a point: both the stored integer and the scaled value (deg C)
gdal raster pixel-info --position-crs WGS84 -f CSV $COG 147,-45

# A region, as it is (xmin,ymin,xmax,ymax)
gdal raster clip --bbox 130,-55,150,-42 $COG tasman.tif

# A region in a local projection, at 1 km
gdal raster reproject -d "+proj=laea +lon_0=147 +lat_0=-45" \
  --bbox -600000,-600000,600000,600000 --resolution 1000,1000 -r bilinear \
  $COG tasman_laea.tif

# The whole world at about 0.25 degree, read from an overview
gdal raster resize --size 1440,0 $COG world_quarter_degree.tif
```

These need GDAL 3.11 or later; the classic utilities (`gdalinfo`, `gdal_translate`,
`gdalwarp`, ...) work with the same inputs on older versions.

A stack of days as the bands of one virtual raster, read lazily:

```bash
for d in 2024-12-29 2024-12-30 2024-12-31; do
  ymd=${d//-/}
  echo "/vsicurl/https://data.source.coop/ausantarctic/ghrsst-mur-v2/${d:0:4}/${d:5:2}/${d:8:2}/${ymd}090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_analysed_sst.tif"
done > days.txt
gdal raster stack @days.txt days.vrt
gdal raster pixel-info --position-crs WGS84 -f CSV days.vrt 147,-45
```

Inside AWS `us-west-2`, read through S3 instead:

```bash
export AWS_NO_SIGN_REQUEST=YES AWS_REGION=us-west-2
gdal raster info /vsis3/us-west-2.opendata.source.coop/ausantarctic/ghrsst-mur-v2/2025/06/01/20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_analysed_sst.tif
```

## DuckDB

The STAC GeoParquet index is an ordinary Parquet file with one row per day.
In the [DuckDB](https://duckdb.org) shell:

```sql
-- How many days of each status, and their date ranges
SELECT "ghrsst:status" AS status, count(*) AS days,
       min(datetime) AS first, max(datetime) AS last
FROM 'https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.parquet'
GROUP BY ALL ORDER BY first;

-- The analysed_sst URLs of every final day in 2024
SELECT datetime, assets.analysed_sst.href AS href
FROM 'https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.parquet'
WHERE "ghrsst:status" = 'final'
  AND datetime >= '2024-01-01' AND datetime < '2025-01-01'
ORDER BY datetime;

-- Days made or re-made since the status fields were added, and when
SELECT datetime, updated, "processing:lineage"
FROM 'https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.parquet'
WHERE updated IS NOT NULL
ORDER BY datetime DESC LIMIT 20;
```

The same queries run from Python, R, or anything else with a DuckDB binding, and any
Parquet reader (Polars, pandas, arrow, Spark) reads the file.

## QGIS

Add a COG as a raster layer from its URL: *Layer > Add Layer > Add Raster Layer*, source
type *Protocol: HTTP(S), cloud, etc.*, and paste the `https://data.source.coop/...tif`
URL. QGIS reads the overviews as you zoom out, so a whole-world view is quick. The
scale and offset are in the layer's *Information* panel (degrees Celsius for
`analysed_sst`).

## In the browser

COGs can be read in a web page with HTTP range requests, no server needed.

### ortho-cog-viewer

[ortho-cog-viewer](https://github.com/mdsumner/ortho-cog-viewer) draws COGs in any
projection, reprojecting on the GPU as you pan. Everything is in the link:

- [A MUR day on an orthographic globe, centred on Tasmania](https://mdsumner.github.io/ortho-cog-viewer/?mode=centred&proj=ortho&center=147,-42&url=preset:mur-sst-20260829)
  (a built-in preset)
- [2025-06-01, any date by its URL](https://mdsumner.github.io/ortho-cog-viewer/?mode=centred&proj=ortho&center=147,-42&url=https%3A%2F%2Fdata.source.coop%2Fausantarctic%2Fghrsst-mur-v2%2F2025%2F06%2F01%2F20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_analysed_sst.tif%23scale%3D0.001%26offset%3D25%26min%3D-2%26max%3D32%26cmap%3Dturbo)
- [The same day over the Southern Ocean, south polar Lambert azimuthal](https://mdsumner.github.io/ortho-cog-viewer/?mode=centred&proj=laea&center=0,-90&zoom=-12.5&url=preset:gibs-bluemarble-3857&url=https%3A%2F%2Fdata.source.coop%2Fausantarctic%2Fghrsst-mur-v2%2F2025%2F06%2F01%2F20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_analysed_sst.tif%23scale%3D0.001%26offset%3D25%26min%3D-2%26max%3D32%26cmap%3Dturbo)

For another date, change the date in the `url=` parameter. The layer URL is
percent-encoded because it carries its own styling after a `#` (`min`, `max` and `cmap`
set the colour scale, in degrees Celsius).

### playproj

[playproj](https://github.com/hypertidy/playproj) shows how rasters are stored and read.
Its *Tiles and overviews* page reads a COG's header (no pixels) and draws its pixel grid,
its tiles and its overview levels on the globe; click a tile for its byte range in the
file. That's the structure every tool on these pages relies on, and that the
[virtual stores](virtual.md) index:

- [A MUR `analysed_sst` COG's tiles and overviews](https://hypertidy.github.io/playproj/structure.html#src=https%3A%2F%2Fdata.source.coop%2Fausantarctic%2Fghrsst-mur-v2%2F2025%2F06%2F01%2F20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_analysed_sst.tif&kind=tiff)

Its *Rasterize* page can use a COG as the grid too, and sums the bytes of the tiles a
polygon touches: how much of a file a region query actually reads.

### Your own page

[geotiff.js](https://geotiffjs.github.io/) reads a window or an overview of a COG in
JavaScript:

```js
import { fromUrl } from "geotiff";

const url = "https://data.source.coop/ausantarctic/ghrsst-mur-v2/2025/06/01/" +
  "20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_analysed_sst.tif";
const tiff = await fromUrl(url);
// The whole world at 720 by 360, from an overview; values are the stored integers
const [raw] = await tiff.readRasters({ bbox: [-180, -90, 180, 90], width: 720, height: 360 });
const degC = Array.from(raw, (v) => (v === -32768 ? NaN : v * 0.001 + 25));
```
