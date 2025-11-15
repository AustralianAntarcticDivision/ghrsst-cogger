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

The [code used to repackage this data is accessible here](https://github.com/auspatious/ghrsst-cogger).

Care has been taken to not change the source data, but just to repackage it into cloud optimised geotiffs with
STAC metadata. Each daily dataset is accessible in a `<year>/<month>/<day>` path, and file names are consistent with the
source data, e.g., `"{$Y%m%d}090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.stac-item.json`.

### Data Variables

| Name | Long Name | Unit |
|------|-----------|------|
| analysed_sst | analysed sea surface temperature | kelvin |
| analysis_error | estimated error standard deviation of analysed_sst | kelvin |
| lat | latitude | degrees_north |
| lon | longitude | degrees_east |
| mask | sea/land field composite mask | - |
| sea_ice_fraction | sea ice area fraction | fraction (between 0 and 1) |
| time | reference time of sst field | seconds since 1981-01-01 00:00:00 UTC |

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
k_to_c = -273.15

sst_scaled = sst_masked * scale + offset + k_to_c
sst_scaled.plot.imshow(size=8, robust=True, cmap="inferno")
```

### Reading from STAC Parquet file

Alternately, you can use the STAC Parquet file as an index to all the STAC docs.

```python
import stacrs
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

items = await stacrs.read(url)

# Or use .search to filter by time
# year = 2024
# items = stacrs.search(
#     url,
#     bbox=bbox,
#     datetime=f"{year}-01-01T00:00:00.000Z/{year}-12-31T23:59:59.999Z",
# )

items = [pystac.Item.from_dict(i) for i in items["features"]]

data = load(items, bbox=bbox, chunks={})
data
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

Strictly, we should check the catalogue first:

``` r
catalog <- arrow::read_parquet("https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.parquet")

if (!url %in% catalog$assets$analysed_sst$href) {
  message("file does not exist, check available dates")
} else {
  dsn <- glue::glue("/vsicurl/{url}")
}
```

Read using the terra package:

``` r
library(terra)
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

## License

See: [https://podaac.jpl.nasa.gov/CitingPODAAC](https://podaac.jpl.nasa.gov/CitingPODAAC)

> Data hosted by the PO.DAAC is openly shared, without restriction, in accordance with NASA's Earth Science program Data and Information Policy.
