"""Write a GDAL multidimensional VRT over every COG in the collection.

The VRT is built from the STAC items alone (no COG is opened): the grid
comes from ``proj:shape`` / ``proj:transform``, the CRS from ``proj:epsg``,
one time step per item datetime, and one array per asset (analysed_sst,
analysis_error, mask, sea_ice_fraction, sst_anomaly) with dtype, nodata,
scale, offset and unit from ``raster:bands``. Days without an asset (e.g.
sst_anomaly before 2014) read as nodata.

GDAL opens it as one (time, y, x) cube per variable, and each source states
its extent, so a read opens only the COGs it touches.
"""

import asyncio
import tempfile
from logging import Logger
from pathlib import Path

import click
import rustac
from s3path import S3Path
from xvrt import from_stac

from ghrsst.cogger import GHRSSTException, get_logger

LOGGER = get_logger(__name__)

VRT_NAME = "ghrsst-mur-v2.vrt"
PARQUET_URL = (
    "https://data.source.coop/ausantarctic/ghrsst-mur-v2/ghrsst-mur-v2.parquet"
)
# GHRSST MUR COGs are written with 512 x 512 tiles; the catalogue does not
# record the block size, so state it.
BLOCK_SIZE = [1, 512, 512]


def write_vrt(items: list[dict], path: str | Path) -> Path:
    """Write the multidim VRT for ``items`` (STAC item dicts) to a local path."""
    if not items:
        raise GHRSSTException("No items to write to VRT")
    return from_stac(items, path, block_size=BLOCK_SIZE)


def publish_vrt(items: list[dict], output_location: str, log: Logger = LOGGER) -> str:
    """Write the VRT next to the parquet: a local folder or ``s3:/bucket/prefix``.

    Built in a temporary file, then copied, so a failure never leaves a
    partial VRT at the published location.
    """
    output_location = output_location.rstrip("/")
    with tempfile.TemporaryDirectory() as tmp:
        local = write_vrt(items, Path(tmp) / VRT_NAME)
        size_mb = local.stat().st_size / 1e6
        if output_location.startswith(("s3:/", "s3://")):
            out = S3Path(output_location.replace("s3://", "/").replace("s3:/", "/"))
            out = out / VRT_NAME
            log.info(f"Copying {size_mb:.1f} MB VRT to S3 at: s3:/{out}")
            out.write_bytes(local.read_bytes())
            return f"s3:/{out}"
        out = Path(output_location) / VRT_NAME
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(local.read_bytes())
        log.info(f"Wrote {size_mb:.1f} MB VRT to {out}")
        return str(out)


async def read_parquet_items(parquet: str) -> list[dict]:
    collection = await rustac.read(parquet)
    return list(collection["features"])


@click.option("--parquet", type=str, default=PARQUET_URL, show_default=True)
@click.option("--output-location", type=str, default="s3://ausantarctic/ghrsst-mur-v2")
@click.command("create-vrt")
def main(parquet, output_location):
    """Rebuild the VRT from the published STAC parquet (no COG is read)."""
    try:
        items = asyncio.run(read_parquet_items(parquet))
        out = publish_vrt(items, output_location.replace("s3://", "s3:/"))
        click.echo(
            f"Wrote VRT over {len(items)} items to {out.replace('s3:/', 's3://')}"
        )
    except GHRSSTException as e:
        print(f"Failed to create VRT with error {e}")
        exit(1)


if __name__ == "__main__":
    main()
