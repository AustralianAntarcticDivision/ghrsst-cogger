# GHRSST Sea Surface Temperature Analysis

The GHRSST Level 4 MUR (Multi-scale Ultra-high Resolution) Global Foundation Sea Surface Temperature Analysis (v4.1) provides
a high-resolution, daily global assessment of ocean surface temperature. This dataset fuses observations from multiple
satellite-borne sensors (including infrared and microwave radiometers) and in situ measurements, offering a robust and
consistent picture of sea surface temperature conditions. The algorithm employs advanced interpolation and blending
techniques to fill gaps caused by cloud cover and sensor discrepancies, resulting in a seamless, 1-kilometer-resolution
global field. The “foundation” temperature represents the sea surface temperature free of diurnal warming effects,
making it particularly valuable for climate studies, numerical weather prediction, and a variety of marine applications.
The dataset is distributed by NASA’s Jet Propulsion Laboratory in collaboration with the Group for High Resolution Sea Surface
Temperature (GHRSST), and is widely used by researchers, operational agencies, and policy-makers for informed decision-making
in fields such as marine resource management, weather forecasting, and climate monitoring.

For more information, please refer to the [documentation](https://podaac.jpl.nasa.gov/dataset/MUR-JPL-L4-GLOB-v4.1).

## About this repository

The [code used to repackage this data is accessible here](https://github.com/AustralianAntarcticDivision/ghrsst-cogger),
and the [documentation](https://australianantarcticdivision.github.io/ghrsst-cogger/) has background and
examples for Python, R, GDAL, DuckDB and the browser.

Care has been taken to not change the source data, but just to repackage it into cloud optimised geotiffs with
STAC metadata. Each daily dataset is accessible in a `<year>/<month>/<day>` path, and file names are consistent with the
source data, e.g. `2025/06/01/20250601090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.stac-item.json`, with one COG
per variable (`..._analysed_sst.tif`, `..._analysis_error.tif`, `..._mask.tif`, `..._sea_ice_fraction.tif` and, where
the source has it, `..._sst_anomaly.tif`).

Every COG is on the same north-up grid in `EPSG:4326`: 36000 by 17999 pixels of 0.01 degree, with its top-left
corner at -179.995, 89.995. Values are the source's integers, with nodata, scale and offset in the metadata. The
COG's own metadata and the STAC item's `raster:bands` agree: `analysed_sst`, `analysis_error` and `sst_anomaly`
unscale to degrees Celsius. The source is in Kelvin; only the offset differs (25 here, 298.15 in the source), so the
integers are unchanged.

## Preliminary and final data

MUR publishes each day twice: a near-real-time (NRT) version about 1 day after the date,
then a final version, usually 9 days after it (occasionally later), which replaces it.
New days are converted from the NRT version as soon as it's available, then re-converted
once the final version is published.

Every STAC item says which version its COGs were made from:

| Property | Meaning |
|---|---|
| `ghrsst:status` | `final`: made from the final version. `preliminary`: made from the NRT version; a final is expected and the COGs will be replaced. `nrt_only`: the archived source is still the NRT version more than 50 days after the date, so no final was published. |
| `processing:lineage` | The source NetCDF's title, e.g. `Daily MUR SST, Final product` |
| `processing:datetime` | When JPL produced the source NetCDF |
| `ghrsst:source_published` | When PO.DAAC published the source NetCDF |
| `processing:version`, `processing:level`, `processing:facility` | MUR product version (`04.1`), level (`L4`) and producer (`NASA JPL`) |
| `processing:software` | Software versions used for the conversion (set when the COGs are made) |
| `updated` | When the COGs were last (re)made |
| `file:checksum`, `file:size` (on each COG asset) | SHA2-256 multihash and size of the COG, from the [File extension](https://github.com/stac-extensions/file) |

The `processing:*` fields follow the [STAC Processing extension](https://github.com/stac-extensions/processing).
Each item also has a `derived_from` link to the source NetCDF. For research that needs the
final analysis, filter on `ghrsst:status = final`.

When a day's final version replaces its NRT version, its COGs are re-made **at the same
URLs**, so the byte offsets inside them change. If you hold byte-range references to these
COGs (kerchunk, virtual Zarr, Icechunk virtual chunks), check `file:checksum` (or the HTTP
`ETag`) and regenerate the references when it changes.

## Accessing data

### Using Python

To load a single day, you can do something like the following using Python:

```python
from pystac import Item
from odc.stac import load

path = "https://data.source.coop/ausantarctic/ghrsst-mur-v2/2025/02/09/20250209090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.stac-item.json"
item = Item.from_file(path)

data = load([item], chunks={}, anchor="center")
data
```

And then to plot it, do:

```python
sst_loaded = data["analysed_sst"].squeeze().compute()
sst_masked = sst_loaded.where(sst_loaded != -32768)

# Extract the scale and offset from the metadata
meta = item.assets["analysed_sst"].extra_fields["raster:bands"][0]
scale = meta["scale"]
offset = meta["offset"]

sst_scaled = sst_masked * scale + offset  # degrees Celsius
sst_scaled.plot.imshow(size=8, robust=True, cmap="inferno")
```

### Reading from STAC Parquet file

Alternately, you can use the STAC Parquet file as an index to all the STAC docs.

```python
import rustac
import pystac
from odc.stac import load

url = "https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.parquet"

center = 13, -61
buffer = 5

bbox = (
    center[1] - buffer,
    center[0] - buffer,
    center[1] + buffer,
    center[0] + buffer,
)

# rustac is async: use await in a notebook, or asyncio.run(...) in a script
items = (await rustac.read(url))["features"]

# Or use .search to filter by place and time, which returns a list of items
# year = 2024
# items = await rustac.search(
#     url,
#     bbox=bbox,
#     datetime=f"{year}-01-01T00:00:00.000Z/{year}-12-31T23:59:59.999Z",
# )

# Keep only days made from the final (not near-real-time) version
items = [i for i in items if i["properties"].get("ghrsst:status") == "final"]

items = [pystac.Item.from_dict(i) for i in items]

data = load(items, bbox=bbox, chunks={})
data
```

### All dates as one cube: multidimensional VRT

`ghrsst-mur-v2.vrt` describes every COG as one GDAL multidimensional dataset:
five `(time, y, x)` arrays (`analysed_sst`, `analysis_error`, `mask`,
`sea_ice_fraction`, `sst_anomaly`) on the shared 0.01 degree grid, one time
step per day from 2002-06-01. It is rebuilt with the STAC Parquet file each
day and holds no data itself: reads go to the COGs, and only the COGs a read
touches are opened. Days without a file for a variable (`sst_anomaly` before
2014) read as nodata.

Scale, offset and units come from the STAC metadata, which matches the COG
headers: `analysed_sst`, `analysis_error` and `sst_anomaly` unscale to degrees
Celsius.

The VRT is one file of about 17 MB, but every day a read touches costs a read of that
day's COG header, so a long time series at one point takes a second or two per day.
Set `GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR`: without it, GDAL also lists each day's
folder and looks for sidecar files next to every COG, which roughly doubles the time
(a 10-day read took 38 s without it and 21 s with it).

```bash
export GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR
gdal mdim info /vsicurl/https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.vrt
```

```python
from osgeo import gdal

gdal.UseExceptions()
gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
vrt = "/vsicurl/https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.vrt"
ds = gdal.OpenEx(vrt, gdal.OF_MULTIDIM_RASTER)
root = ds.GetRootGroup()
sst = root.OpenMDArray("analysed_sst").GetUnscaled()  # celsius

# The last 30 days at 44S 150E (about a minute: 30 COG headers), finding the row and
# column from the coordinates
y = root.OpenMDArray("y").ReadAsArray()
x = root.OpenMDArray("x").ReadAsArray()
row, col = int(abs(y + 44).argmin()), int(abs(x - 150).argmin())
n = sst.GetDimensions()[0].GetSize()
series = sst.ReadAsArray(array_start_idx=[n - 30, row, col], count=[30, 1, 1]).ravel()
print(series)
```

Any reader of GDAL's multidimensional API can open it, for example an xarray
backend for GDAL:

```python
import xarray as xr

ds = xr.open_dataset(vrt, engine="gdalxarray")
ds.analysed_sst.sel(time="2025-02-14").sel(y=-44, x=150, method="nearest")
```

### Using R

The 'analysed_sst' files are templated by:

``` r
template <- "https://data.source.coop/ausantarctic/ghrsst-mur-v2/{format(date, '%Y/%m/%d')}/{format(date, '%Y%m%d')}090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1_analysed_sst.tif"
```

Example for a specific date:

``` r
date <- as.Date("2025-02-14")
(url <- glue::glue(template))
```

Strictly, we should check the catalogue first, which also says whether the day is final:

``` r
catalog <- arrow::read_parquet("https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.parquet")

i <- match(url, catalog$assets$analysed_sst$href)
if (is.na(i)) {
  message("file does not exist, check available dates")
} else {
  catalog$`ghrsst:status`[i]  # "final", "preliminary" or "nrt_only"
  dsn <- glue::glue("/vsicurl/{url}")
}
```

Read using the terra package (which applies the scale and offset, giving degrees Celsius):

``` r
library(terra)
setGDALconfig("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
(sst <- rast(dsn))
```

Or alternatively, using gdalraster:

``` r
library(gdalraster)
(ds <- new(GDALRaster, dsn))
```

Or with stars:

``` r
library(stars)
(sstars <- read_stars(dsn, proxy = TRUE))
```

To crop with the terra package:

``` r
crop(sst, ext(130, 150, -55, -42))
```

Or, more generally, project (by_util is very important for COG efficiency):

``` r
project(sst, rast(ext(130, 150, -55, -42), res = res(sst)), by_util = TRUE)
```

We can also change CRS and resolution as desired:

``` r
target <- rast(
  ext(c(-1, 1, -1, 1) * 6e5),
  res = 1000,
  crs = "+proj=laea +lon_0=147 +lat_0=-45"
)
project(sst, target, by_util = TRUE)
```

## Coming: more virtual indexes

Besides the multidimensional VRT above, more indexes that open the whole archive as one array per variable,
without copying the data, will be published alongside the COGs: a flat tabular index of every tile (in addition
to the STAC GeoParquet), kerchunk Parquet references and an Icechunk store. See
[Virtual stores](https://australianantarcticdivision.github.io/ghrsst-cogger/virtual.html).

## License

The source is the GHRSST Level 4 MUR Global Foundation Sea Surface Temperature Analysis (v4.1), NASA JPL,
distributed by PO.DAAC, [doi:10.5067/GHGMR-4FJ04](https://doi.org/10.5067/GHGMR-4FJ04). To cite it, see:
[https://podaac.jpl.nasa.gov/CitingPODAAC](https://podaac.jpl.nasa.gov/CitingPODAAC)

> Data hosted by the PO.DAAC is openly shared, without restriction, in accordance with NASA's Earth Science program Data and Information Policy.
