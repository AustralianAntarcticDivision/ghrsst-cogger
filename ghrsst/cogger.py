#!/usr/bin/env python3

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime
from logging import Logger
from pathlib import Path
from typing import Any, TypeGuard

import boto3
import click
import fsspec
import xarray as xr
from affine import Affine
from aiohttp.client_exceptions import ClientResponseError
from botocore.exceptions import ClientError
from earthaccess import get_edl_token, login
from odc.geo.geobox import GeoBox
from odc.geo.xr import assign_crs, xr_coords
from pystac import Asset, Item, Link, MediaType, RelType
from rio_stac import create_stac_item
from s3path import S3Path
from xarray import Dataset

COLLECTION = "ghrsst-mur-v2"
FILE_STRING = "{date:%Y%m%d}090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.nc"
FOLDER_PATH = "{date:%Y}/{date:%m}/{date:%d}"
JPL_BASE = "https://archive.podaac.earthdata.nasa.gov/podaac-ops-cumulus-protected/MUR-JPL-L4-GLOB-v4.1/"
VARIABLES = [
    "analysed_sst",
    "analysis_error",
    "mask",
    "sea_ice_fraction",
    "sst_anomaly",
    "dt_1km_data",
]
# Not loading or writing the datetime data
DROP_VARIABLES = ["dt_1km_data"]
VARIABLES = [var for var in VARIABLES if var not in DROP_VARIABLES]
COG_OPTS: dict[str, Any] = dict(compress="zstd")


class GHRSSTException(Exception):
    """A base class for GHRSSTException exceptions."""


@contextmanager
def environ(env):
    """Temporarily set environment variables inside the context manager and
    fully restore previous environment afterwards
    """
    original_env = {key: os.getenv(key) for key in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for key, value in original_env.items():
            if value is None:
                del os.environ[key]
            else:
                os.environ[key] = value


def _is_s3_path(path: Path | S3Path) -> TypeGuard[S3Path]:
    return isinstance(path, S3Path)


def _exists(path: Path | S3Path) -> bool:
    if _is_s3_path(path):
        try:
            s3 = boto3.client("s3")
            s3.head_object(Bucket=path.bucket, Key=path.key)
            return True
        except ClientError:
            return False
    else:
        return path.exists()


def _get_href(path: Path | S3Path) -> str:
    href = str(path)
    if _is_s3_path(path):
        # Assume we're writing to source.coop
        if "us-west-2.opendata.source.coop" in str(path):
            href = f"https://data.source.coop{str(path).replace('/us-west-2.opendata.source.coop', '')}"
        else:
            href = f"s3:/{path}"

    return href


def get_logger(name: str = __name__) -> Logger:
    logger = logging.getLogger(name)
    # Only add the handler once, as warm Lambda containers call this again
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    return logger


# Module logger, used as the default for functions that take a log argument
LOGGER = get_logger()


def get_output_path(output_location: Path | S3Path, date: datetime, extension: str):
    return (
        output_location
        / FOLDER_PATH.format(date=date)
        / FILE_STRING.format(date=date).replace(".nc", extension)
    )


def get_input_path(input_location: str, date: datetime) -> str:
    if input_location.upper() == "JPL":
        return JPL_BASE + FILE_STRING.format(date=date)
    else:
        return str(Path(input_location) / FILE_STRING.format(date=date))


def get_headers() -> dict[str, str]:
    """Get the earthdata authorisation headers for the request"""
    earthdata_token = os.environ.get("EARTHDATA_TOKEN", None)
    earthdata_password = os.environ.get("EARTHDATA_PASSWORD", None)

    # Do some basic checks
    if earthdata_token is None and earthdata_password is None:
        raise GHRSSTException(
            "Please set one of EARTHDATA_TOKEN or EARTHDATA_PASSWORD environment variables"
        )
    if earthdata_password is not None:
        if os.environ.get("EARTHDATA_USERNAME") is None:
            raise GHRSSTException("Please set EARTHDATA_USERNAME environment variable")

    # If we don't have a token, use the username and password to get one
    if earthdata_token is None:
        auth = login(strategy="environment")
        if auth.authenticated:
            # earthaccess annotates get_edl_token() as str, but it returns a dict
            earthdata_token = get_edl_token()["access_token"]  # ty: ignore[invalid-argument-type]
        else:
            raise GHRSSTException("Failed to authenticate with Earthdata")

    # Return the headers
    return {"Authorization": f"Bearer {earthdata_token}"}


def get_simple_raster_info(data: Dataset, var: str):
    variable = data[var]

    scale = variable.attrs.get("scale_factor")
    offset = variable.attrs.get("add_offset")
    unit = variable.attrs.get("units")

    meta = {
        "nodata": int(variable.attrs["_FillValue"]),
        "data_type": str(variable.dtype.name),
    }

    if scale is not None:
        meta["scale"] = float(scale)
    if offset is not None:
        meta["offset"] = float(offset)
    if unit is not None:
        meta["unit"] = str(unit)

    return [meta]


def load_data(
    date: datetime, input_location: str, cache_local: bool = False, log: Logger = LOGGER
) -> Dataset:
    input_path = get_input_path(input_location, date)

    if cache_local:
        log.info(f"Caching {input_path} locally")
        cache_path = Path("/tmp") / FILE_STRING.format(date=date)
        with fsspec.open(input_path, headers=get_headers()) as f:
            with cache_path.open("wb") as cache_f:
                cache_f.write(f.read())
        data = xr.open_dataset(
            cache_path, chunks={}, mask_and_scale=False, drop_variables=DROP_VARIABLES
        )
    elif input_location.upper() == "JPL":
        # Open the file
        try:
            with fsspec.open(input_path, headers=get_headers()) as f:
                data = xr.open_dataset(
                    f,
                    mask_and_scale=False,
                    drop_variables=DROP_VARIABLES,
                    engine="h5netcdf",
                ).load()
        except ClientResponseError as e:
            raise GHRSSTException(
                f"Failed to open {input_path} with error {e}. Please check your EARTHDATA_TOKEN."
            ) from e
        # Don't catch this here. Catch it elsewhere and handle differently.
        # except FileNotFoundError as e:
        #     raise GHRSSTException(f"File not found for {input_path} with error {e}")
    else:
        data = xr.open_dataset(
            input_path, mask_and_scale=False, drop_variables=DROP_VARIABLES
        )

    return data


def process_data(data: Dataset) -> Dataset:
    # Assign the CRS
    data = assign_crs(data, crs="EPSG:4326")

    # Set up a new Affine and GeoBox
    new_affine = Affine(0.01, 0.0, -179.995, 0.0, -0.01, 89.995, 0.0, 0.0, 1.0)
    new_geobox = GeoBox(data.odc.geobox.shape, new_affine, data.odc.geobox.crs)
    new_coords = xr_coords(new_geobox)

    # First flip the dataset vertically
    data = data.reindex(lat=data.lat[::-1])

    # Update the coordinates of the xarray to be precise
    data = data.assign_coords(new_coords)

    # Now update coords for each data variable
    for var in data.data_vars:
        data[var].odc.reload()

    # Update SST metadata to be in celcius
    data["analysed_sst"].attrs["units"] = "celsius"
    data["analysed_sst"].attrs["add_offset"] = 25
    data["analysis_error"].attrs["units"] = "celsius"

    if "sst_anomaly" in data.data_vars:
        data["sst_anomaly"].attrs["units"] = "celsius"

    return data


def write_data(
    data: Dataset,
    date: datetime,
    output_location: Path | S3Path,
    overwrite: bool = False,
    log: Logger = LOGGER,
) -> list[tuple[str, Path | S3Path]]:
    if not _is_s3_path(output_location):
        if not output_location.exists():
            output_location.mkdir(parents=True)

    data = data.chunk({"time": 1, "lat": 500, "lon": 500})

    written_files: list[tuple[str, Path | S3Path]] = []
    # xarray types variable names as Hashable; ours are always str
    for var in map(str, data.data_vars):
        cog_file = get_output_path(output_location, date, f"_{var}.tif")

        if _exists(cog_file) and not overwrite:
            log.info(f"Skipping {var} as it already exists")
            written_files.append((var, cog_file))
            continue

        data_var = data[var]
        # Rename to GDAL/ODC standard names
        data_var.attrs["scales"] = data_var.attrs.get("scale_factor")
        data_var.attrs["offsets"] = data_var.attrs.get("add_offset")
        data_var.attrs["units"] = data_var.attrs.get("units")
        data_var.attrs["nodata"] = data_var.attrs.get("_FillValue")

        cog_path_str = str(cog_file)
        if not _is_s3_path(cog_file.parent):
            cog_file.parent.mkdir(parents=True, exist_ok=True)
        else:
            cog_path_str = f"s3:/{cog_file}"

        log.info(f"Writing {var} to {cog_path_str}")

        # Stream direct to S3
        # cog = save_cog_with_dask(data_var, cog_path_str, **COG_OPTS)
        # cog.compute()

        # Use the public method. This does _NOT_ write the correct
        # geotransform... TODO: report and resolve.
        # from odc.geo.cog import write_cog
        # cog_file.write_bytes(write_cog(data_var, ":mem:", **COG_OPTS))

        # Use the private method, forcing the geobox
        from odc.geo.cog._rio import _get_gdal_metadata, _write_cog

        # The private API is annotated as taking an ndarray but accepts a DataArray
        cog_file.write_bytes(
            _write_cog(
                data_var,  # ty: ignore[invalid-argument-type]
                data.odc.geobox,
                ":mem:",
                nodata=data_var.attrs.get("nodata"),
                gdal_metadata=_get_gdal_metadata(data_var, {}),
                **COG_OPTS,
            )
        )

        log.info(f"Finished writing {var}")

        written_files.append((var, cog_file))

    return written_files


def write_stac(
    data: Dataset,
    written_files: list[tuple[str, Path | S3Path]],
    date: datetime,
    output_location: Path | S3Path,
    log: Logger = LOGGER,
) -> Item:
    stac_file = get_output_path(output_location, date, ".stac-item.json")

    first_item = written_files[0][1]
    href = _get_href(first_item)

    log.info(f"Writing STAC based on {href}")

    item = create_stac_item(
        href,
        id=stac_file.stem,
        collection="ghrsst-mur-v2",
        input_datetime=date,
        with_proj=True,
        with_raster=True,
        properties={
            "start_datetime": f"{date:%Y-%m-%d}T00:00:00Z",
            "end_datetime": f"{date:%Y-%m-%d}T23:59:59Z",
        },
        assets={
            var: Asset(
                href=_get_href(file),
                title=var,
                media_type=MediaType.COG,
                roles=["data"],
                extra_fields={"raster:bands": get_simple_raster_info(data, var)},
            )
            for var, file in written_files
        },
    )

    item.add_link(
        Link(
            rel=RelType.CANONICAL,
            target=get_input_path("JPL", date),
            media_type=MediaType.NETCDF,
            title="Original NetCDF",
        )
    )

    if _is_s3_path(output_location):
        # Assume we're writing to source.coop
        item.set_self_href(_get_href(stac_file))
        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=stac_file.bucket,
            Key=stac_file.key,
            Body=json.dumps(item.to_dict(), indent=2),
            ACL="bucket-owner-full-control",
            ContentType="application/json",
        )
    else:
        item.set_self_href(str(stac_file))
        item.save_object()

    return item


def get_context() -> dict[str, str]:
    # Deal with the case where we're writing to source coop
    sc_endpoint = os.environ.get("SOURCECOOP_AWS_ENDPOINT_URL")
    sc_access_key = os.environ.get("SOURCECOOP_AWS_ACCESS_KEY_ID")
    sc_secret_key = os.environ.get("SOURCECOOP_AWS_SECRET_ACCESS_KEY")

    context = {}
    if (
        sc_endpoint is not None
        and sc_access_key is not None
        and sc_secret_key is not None
    ):
        context = {
            "AWS_ENDPOINT_URL": sc_endpoint,
            "AWS_ACCESS_KEY_ID": sc_access_key,
            "AWS_SECRET_ACCESS_KEY": sc_secret_key,
        }

    return context


def process_date(
    date: datetime,
    input_location: str,
    output_location: Path | S3Path,
    overwrite: bool = False,
    cache_local: bool = False,
    log: Logger = LOGGER,
):
    """Process a date from a data source and output to a location

    Args:
        date (datetime): Date to process
        input_location (str): Either 'jpl' to grab data from JPL, or a local folder to find the data in
        output_location (str): Location to output results to
    """
    log.info(
        f"Processing date {date:%Y-%m-%d} from {input_location} to {output_location}"
    )

    log.info(f"Overwrite: {overwrite}, Cache Local: {cache_local}")

    # Switch up our environment, in case we need to work on source.coop
    context = get_context()

    with environ(context):
        # Check if we've done this date already
        stac_file = get_output_path(output_location, date, ".stac-item.json")
        if _exists(stac_file) and not overwrite:
            log.info(f"Skipping {date:%Y-%m-%d} as it already exists")
        else:
            input_path = get_input_path(input_location, date)
            log.info(f"Loading data from {input_path}")
            data = load_data(date, input_location, cache_local=cache_local, log=log)

            log.info("Processing data...")
            processed = process_data(data)

            if _is_s3_path(output_location):
                log.info(f"Writing data to s3:/{output_location}")
            else:
                log.info(f"Writing data to {output_location}")
            written_files = write_data(
                processed, date, output_location, overwrite, log=log
            )

            log.info("Writing STAC")
            stac_doc = write_stac(data, written_files, date, output_location, log=log)

            # Cleanup
            if cache_local:
                log.info("Cleaning up cache")
                cache_path = Path("/tmp") / FILE_STRING.format(date=date)
                cache_path.unlink()

            log.info(f"Finished writing to: {stac_doc.self_href}")


def lambda_handler(event, _):
    # Set up a tidy logger, but try to use the AWS way of logging
    log = LOGGER
    log.info(f"Event: {event}")

    date_str = None
    for record in event["Records"]:
        event_source = record.get("eventSource")
        if event_source is not None and event_source == "aws:sqs":
            message = json.loads(record["body"])
            log.info(message)
            # Get the date from the message
            date_str = message["date"]
        else:
            log.error(f"No SQS message found, only {record}")

    if date_str is not None:
        date = datetime.strptime(date_str, "%Y-%m-%d")
        output_location = os.environ.get(
            "OUTPUT_LOCATION", "s3://files.auspatious.com/ghrsst/"
        )
        output_location = S3Path(output_location.replace("s3://", "/"))
        input_location = "JPL"
        overwrite = os.environ.get("OVERWRITE", "False").lower() == "true"
        cache_local = os.environ.get("CACHE_LOCAL", "False").lower() == "true"

        try:
            process_date(
                date,
                input_location,
                output_location,
                overwrite,
                cache_local=cache_local,
                log=log,
            )
        except FileNotFoundError as e:
            log.error(f"Couldn't find file for date {date:%Y-%m-%d} with error {e}")
    else:
        raise GHRSSTException("No date found in event, exiting")


@click.option("--date", type=str)
@click.option("--output-location", type=str)
@click.option("--input-location", type=str, default="JPL")
@click.option("--overwrite/--no-overwrite", is_flag=True, default=False)
@click.option("--cache-local/--no-cache-local", is_flag=True, default=False)
@click.command("ghrsst-cogger")
def main(date, output_location, input_location, overwrite, cache_local):
    date = datetime.strptime(date, "%Y-%m-%d")

    if output_location.startswith("s3://"):
        output_location = S3Path(output_location.replace("s3://", "/"))
    else:
        output_location = Path(output_location)

    # Only catch known exceptions, and otherwise let the program crash
    try:
        process_date(
            date, input_location, output_location, overwrite, cache_local=cache_local
        )
    except GHRSSTException as e:
        print(f"Failed to process date {date:%Y-%m-%d} with error {e}")
        exit(1)
    except FileNotFoundError as e:
        print(f"Couldn't find file for date {date:%Y-%m-%d} from {e}")


if __name__ == "__main__":
    main()
