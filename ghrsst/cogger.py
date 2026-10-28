#!/usr/bin/env python3

import functools
import hashlib
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from importlib import metadata
from logging import Logger
from pathlib import Path
from typing import Any, TypeGuard

import boto3
import click
import fsspec
import h5netcdf
import requests
import xarray as xr
from affine import Affine
from aiohttp.client_exceptions import ClientResponseError
from boto3.session import Session
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from odc.geo.geobox import GeoBox
from odc.geo.xr import assign_crs, xr_coords
from pystac import Asset, Item, Link, MediaType, RelType
from requests.adapters import HTTPAdapter
from rio_stac import create_stac_item
from s3path import S3Path
from urllib3.util.retry import Retry
from xarray import Dataset

COLLECTION = "ghrsst-mur-v2"
FILE_STRING = "{date:%Y%m%d}090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.nc"
FOLDER_PATH = "{date:%Y}/{date:%m}/{date:%d}"
# The source's sea_ice_fraction unit, which newer source files leave out
SEA_ICE_UNIT = "fraction (between 0 and 1)"
# Where cache_local downloads NetCDFs. Lambda's only writable disk, and it persists
# between invocations on a warm container
CACHE_DIR = Path("/tmp")
JPL_BASE = "https://archive.podaac.earthdata.nasa.gov/podaac-ops-cumulus-protected/MUR-JPL-L4-GLOB-v4.1/"
CMR_URL = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
CMR_SHORT_NAME = "MUR-JPL-L4-GLOB-v4.1"
# CMR's largest page. A response at this size may be truncated
CMR_PAGE_SIZE = 2000
# The cogger downloads the NetCDF at most one Lambda run (15 min) before it writes the
# COG. A COG written less than this after the NetCDF was published may have been made
# from the previous version, so it's too close to call and gets reprocessed
CLOSE_CALL_HOURS = 1
CLOSE_CALL = timedelta(hours=CLOSE_CALL_HOURS)
# An NRT NetCDF this long after its date won't be replaced by a final: JPL never
# finalised it, so re-COGing it changes nothing. Finals have arrived up to ~40 days
# late (2020), so 50 days leaves a margin. A later final would still be caught: its
# COG would then be older than the NetCDF, so preliminary
NRT_ONLY_AFTER = timedelta(days=50)

# Fixed reasons returned by classify, also used to decide what the Lambda does
REASON_NO_DATA = "No COG and no CMR granule"
REASON_NO_COG = "No COG"
REASON_NO_CMR = "No CMR record to compare with"
REASON_REGENERATED = "NetCDF regenerated after COG"
REASON_PUBLISHED_UNKNOWN = "Couldn't read NetCDF publication time"
REASON_PUBLISHED_AFTER = "NetCDF published after COG"
REASON_TOO_CLOSE = f"COG written within {CLOSE_CALL_HOURS} h of NetCDF publication"
REASON_FINAL_SOURCE = "COG written from final NetCDF"
REASON_NRT_ONLY = "Archived NetCDF is still NRT; no final published"
REASON_NRT_SOURCE = "COG written from NRT NetCDF"
REASON_TITLE_UNREADABLE = "Couldn't read NetCDF title"
REASON_TITLE_UNKNOWN = "Unrecognised NetCDF title"

# STAC: a filterable status label, plus provenance in the Processing extension
STATUS_PROPERTY = "ghrsst:status"
PROCESSING_EXTENSION = "https://stac-extensions.github.io/processing/v1.3.0/schema.json"
# file:checksum and file:size on each COG asset, so anyone holding byte-range
# references (kerchunk, virtual Zarr, Icechunk) can tell when a COG was replaced
FILE_EXTENSION = "https://stac-extensions.github.io/file/v2.1.0/schema.json"
# NetCDF global attributes recorded as provenance
SOURCE_ATTRS = ("title", "product_version", "processing_level")
# Connect timeout first: requests tries each of a host's addresses in turn, so a short
# connect timeout falls back from a dead IPv6 route to IPv4 in seconds, not ~75 s
CONNECT_TIMEOUT = 5
# Shared HTTP session for CMR that retries transient server errors.
# raise_on_status=False hands the last response back so raise_for_status can report it
HTTP = requests.Session()
HTTP.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=3,
            backoff_factor=5,
            status_forcelist=(500, 502, 503, 504),
            raise_on_status=False,
        ),
        pool_maxsize=16,
    ),
)
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


_PUBLIC_S3_LOCK = threading.Lock()


def _public_s3():
    # ponytail: unsigned and us-west-2 only, fine for the public source.coop bucket.
    # Pass a signed client in if a private bucket ever needs auditing.
    # One client, shared by the lookup threads: clients are thread-safe, but creating
    # them isn't, so the first call is locked and uses its own session
    with _PUBLIC_S3_LOCK:
        return _make_public_s3()


@functools.cache
def _make_public_s3():
    return Session().client(
        "s3", region_name="us-west-2", config=Config(signature_version=UNSIGNED)
    )


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


EDL_TOKEN_URL = "https://urs.earthdata.nasa.gov/api/users/find_or_create_token"


@functools.cache
def _earthdata_token() -> str:
    """An Earthdata Login bearer token for EARTHDATA_USERNAME / EARTHDATA_PASSWORD.

    Reuses the user's existing token if there is one. This is the same request
    earthaccess.login makes, without also setting up earthaccess's data store, which
    isn't needed here. Cached per process (only the token, not the credentials), so a
    warm Lambda logs in once; tokens are valid for 60 days. A failed login raises, and
    isn't cached.
    """
    try:
        response = HTTP.post(
            EDL_TOKEN_URL,
            auth=(os.environ["EARTHDATA_USERNAME"], os.environ["EARTHDATA_PASSWORD"]),
            headers={"Accept": "application/json"},
            timeout=(CONNECT_TIMEOUT, 60),
        )
        response.raise_for_status()
        return response.json()["access_token"]
    except (requests.RequestException, KeyError, ValueError) as e:
        raise GHRSSTException(f"Earthdata login failed: {e}") from e


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
        earthdata_token = _earthdata_token()

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


def download_netcdf(url: str, path: Path) -> None:
    """Download a NetCDF from PO.DAAC with the Earthdata login, streaming to disk.

    Written to a .part file and renamed when complete, so an interrupted download
    never leaves a truncated file at the final path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    with fsspec.open(url, headers=get_headers()) as src, partial.open("wb") as dst:
        shutil.copyfileobj(src, dst, length=2**24)
    partial.replace(path)


def load_data(
    date: datetime, input_location: str, cache_local: bool = False, log: Logger = LOGGER
) -> Dataset:
    """Open the MUR NetCDF for a date, without applying scale/offset or masking.

    Three ways of loading, picked by the arguments:

    - ``cache_local``: download the whole file to /tmp (with Earthdata auth), then open
      it lazily with dask chunks. Used by the Lambda, and the fastest.
    - ``input_location="JPL"`` without ``cache_local``: stream the file over HTTP and
      load it fully into memory.
    - A local folder without ``cache_local``: open the file from that folder lazily,
      without dask chunks. Used by the tests and ``make run-local``.

    Args:
        date (datetime): Date of the file to open
        input_location (str): 'JPL' to fetch from PO.DAAC, or a local folder holding the file
        cache_local (bool): Download the file to /tmp before opening it
        log (Logger): Logger to use
    """
    input_path = get_input_path(input_location, date)

    if cache_local:
        log.info(f"Caching {input_path} locally")
        cache_path = CACHE_DIR / FILE_STRING.format(date=date)
        download_netcdf(input_path, cache_path)
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
                f"Failed to open {input_path} with error {e}. Please check your Earthdata credentials."
            ) from e
        # Don't catch this here. Catch it elsewhere and handle differently.
        # except FileNotFoundError as e:
        #     raise GHRSSTException(f"File not found for {input_path} with error {e}")
    else:
        # TODO: try chunks={} as in the cache_local branch for performance improvement
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

    # Newer source files (2014 on) leave this out, older ones have it
    data["sea_ice_fraction"].attrs.setdefault("units", SEA_ICE_UNIT)

    return data


def write_data(
    data: Dataset,
    date: datetime,
    output_location: Path | S3Path,
    overwrite: bool = False,
    log: Logger = LOGGER,
) -> list[tuple[str, Path | S3Path, dict[str, Any]]]:
    """Write a COG per variable. Returns (variable, path, File extension fields).

    The File extension fields (checksum and size) are only known for COGs written
    now, so they're empty for existing COGs that were skipped.
    """
    if not _is_s3_path(output_location):
        if not output_location.exists():
            output_location.mkdir(parents=True)

    data = data.chunk({"time": 1, "lat": 500, "lon": 500})

    written_files: list[tuple[str, Path | S3Path, dict[str, Any]]] = []
    # xarray types variable names as Hashable; ours are always str
    for var in map(str, data.data_vars):
        cog_file = get_output_path(output_location, date, f"_{var}.tif")

        if _exists(cog_file) and not overwrite:
            log.info(f"Skipping {var} as it already exists")
            written_files.append((var, cog_file, {}))
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
        cog_bytes = _write_cog(
            data_var,  # ty: ignore[invalid-argument-type]
            data.odc.geobox,
            ":mem:",
            nodata=data_var.attrs.get("nodata"),
            gdal_metadata=_get_gdal_metadata(data_var, {}),
            **COG_OPTS,
        )
        # Writing to ":mem:" returns the COG's bytes (it returns a Path for files)
        if not isinstance(cog_bytes, bytes):
            raise GHRSSTException(f"Expected COG bytes for {var}, got {cog_bytes!r}")
        cog_file.write_bytes(cog_bytes)

        log.info(f"Finished writing {var}")

        written_files.append((var, cog_file, file_fields(cog_bytes)))

    return written_files


def file_fields(content: bytes) -> dict[str, Any]:
    """File extension fields for a written file: SHA2-256 multihash and size."""
    # Multihash: 0x12 = sha2-256, 0x20 = 32-byte digest
    return {
        "file:checksum": "1220" + hashlib.sha256(content).hexdigest(),
        "file:size": len(content),
    }


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def software_versions() -> dict[str, str]:
    """Versions of the software that converts NetCDF to COG, for processing:software."""
    import rasterio

    versions = {"GDAL": rasterio.__gdal_version__}
    for package in ("ghrsst-cogger", "rasterio", "odc-geo", "xarray", "h5netcdf"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "unknown"
    return versions


def source_provenance(
    produced: datetime | None,
    published: datetime | None,
    attrs: dict[str, str] | None,
    converting: bool,
) -> dict[str, Any]:
    """STAC properties describing the NetCDF a COG was made from.

    Uses the Processing extension where it has a field, and ghrsst:source_published
    (when PO.DAAC published the NetCDF) where it doesn't. processing:software is only
    set when converting: a status-only update can't know what made the existing COG.
    """
    props: dict[str, Any] = {"processing:facility": "NASA JPL"}
    if produced:
        props["processing:datetime"] = _rfc3339(produced)
    if published:
        props["ghrsst:source_published"] = _rfc3339(published)
    attrs = attrs or {}
    for attr, field in (
        ("title", "processing:lineage"),
        ("product_version", "processing:version"),
        ("processing_level", "processing:level"),
    ):
        if attr in attrs:
            props[field] = attrs[attr]
    if converting:
        props["processing:software"] = software_versions()
    return props


def apply_status(doc: dict, status: str, provenance: dict[str, Any]) -> dict:
    """Set a STAC item dict's status and provenance, in place, and return it.

    Also adds the Processing extension and changes the source NetCDF link from
    canonical (the authoritative copy of this item) to derived_from (its source).
    """
    doc.setdefault("properties", {})[STATUS_PROPERTY] = status
    doc["properties"].update(provenance)
    extensions = doc.setdefault("stac_extensions", [])
    if PROCESSING_EXTENSION not in extensions:
        extensions.append(PROCESSING_EXTENSION)
    for link in doc.get("links", []):
        if link.get("rel") == "canonical" and link.get("title") == "Original NetCDF":
            link["rel"] = "derived_from"
    return doc


def _read_json(path: Path | S3Path) -> dict | None:
    """A JSON document from S3 or disk, or None if it doesn't exist."""
    if _is_s3_path(path):
        try:
            body = boto3.client("s3").get_object(Bucket=path.bucket, Key=path.key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        return json.loads(body["Body"].read())
    return json.loads(path.read_text()) if path.exists() else None


def _write_json(path: Path | S3Path, doc: dict) -> None:
    """Write a JSON document to S3 (source.coop) or disk."""
    body = json.dumps(doc, indent=2)
    if _is_s3_path(path):
        boto3.client("s3").put_object(
            Bucket=path.bucket,
            Key=path.key,
            Body=body,
            ACL="bucket-owner-full-control",
            ContentType="application/json",
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


def set_stac_status(
    stac_file: Path | S3Path, status: str, provenance: dict[str, Any]
) -> None:
    """Update an existing STAC item's status and provenance, leaving its COGs alone."""
    doc = _read_json(stac_file)
    if doc is None:
        raise GHRSSTException(f"No STAC item at {stac_file} to update")
    _write_json(stac_file, apply_status(doc, status, provenance))


def write_stac(
    data: Dataset,
    written_files: list[tuple[str, Path | S3Path, dict[str, Any]]],
    date: datetime,
    output_location: Path | S3Path,
    status: str,
    provenance: dict[str, Any],
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
                extra_fields={
                    "raster:bands": get_simple_raster_info(data, var),
                    **fields,
                },
            )
            for var, file, fields in written_files
        },
    )

    item.add_link(
        Link(
            rel=RelType.DERIVED_FROM,
            target=get_input_path("JPL", date),
            media_type=MediaType.NETCDF,
            title="Original NetCDF",
        )
    )
    item.properties[STATUS_PROPERTY] = status
    item.properties.update(provenance)
    # When these COGs were (re)made, so replacements are easy to find
    item.properties["updated"] = _rfc3339(datetime.now(UTC))
    item.stac_extensions.append(PROCESSING_EXTENSION)
    if any(fields for _, _, fields in written_files):
        item.stac_extensions.append(FILE_EXTENSION)

    # Assume S3 is source.coop
    item.set_self_href(
        _get_href(stac_file) if _is_s3_path(stac_file) else str(stac_file)
    )
    _write_json(stac_file, item.to_dict())

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


def parse_production_times(umm_json: dict) -> dict[date, datetime]:
    """Latest NetCDF ProductionDateTime per granule date from a CMR umm_json response.

    Keyed by the date in GranuleUR, not the temporal extent: MUR's window for day D
    starts at 21:00 on D-1, so a one-day query also returns the next day's granule.
    """
    times: dict[date, datetime] = {}
    for item in umm_json["items"]:
        umm = item["umm"]
        day = datetime.strptime(umm["GranuleUR"][:8], "%Y%m%d").date()
        produced = datetime.fromisoformat(umm["DataGranule"]["ProductionDateTime"])
        # Keep the latest if CMR has more than one record for a granule
        times[day] = max(produced, times.get(day, produced))
    return times


def check_cmr_complete(umm_json: dict, start: date, end: date) -> None:
    """Raise if CMR returned fewer granule records than it counted as hits.

    Two causes, told apart by the page size. A full page means CMR truncated the
    results, so the range is too long. A short page means CMR counts granules it
    can't return: its index lags behind a PO.DAAC republish, which clears by itself.
    """
    hits, returned = umm_json["hits"], len(umm_json["items"])
    if hits <= returned:
        return
    if returned >= CMR_PAGE_SIZE:
        raise GHRSSTException(
            f"CMR returned {returned} of {hits} granules for {start} to {end} "
            f"(its {CMR_PAGE_SIZE}-granule page limit); query a shorter range"
        )
    raise GHRSSTException(
        f"CMR counts {hits} granules for {start} to {end} but returned {returned} "
        "records. Its index is probably behind a PO.DAAC republish; try again later"
    )


def get_production_times(start: date, end: date) -> dict[date, datetime]:
    """When each MUR NetCDF from start to end (inclusive) was last produced, from CMR."""
    try:
        response = HTTP.get(
            CMR_URL,
            params={
                "short_name": CMR_SHORT_NAME,
                "temporal": f"{start:%Y-%m-%d}T00:00:00Z,{end:%Y-%m-%d}T23:59:59Z",
                "page_size": CMR_PAGE_SIZE,
            },
            timeout=(CONNECT_TIMEOUT, 120),
        )
        response.raise_for_status()
    except (requests.Timeout, requests.ConnectionError) as e:
        raise GHRSSTException(
            f"NASA CMR didn't respond ({type(e).__name__}). It's probably temporarily "
            "down or overloaded, so try again later."
        ) from e
    except requests.HTTPError as e:
        if response.status_code >= 500:
            raise GHRSSTException(
                f"NASA CMR returned HTTP {response.status_code}, a problem on its side. "
                "Try again later."
            ) from e
        raise
    umm_json = response.json()
    check_cmr_complete(umm_json, start, end)
    return parse_production_times(umm_json)


def _attrs_as_str(attrs) -> dict[str, str]:
    """The SOURCE_ATTRS present in a NetCDF's global attributes, as strings."""
    found = {}
    for name in SOURCE_ATTRS:
        value = attrs.get(name)
        if isinstance(value, bytes):
            value = value.decode()
        if value is not None and str(value):
            found[name] = str(value)
    return found


def get_source_attrs(day: date, headers: dict[str, str]) -> dict[str, str] | None:
    """The current MUR NetCDF's title, product version and processing level.

    Read from the archived file itself with HTTP range requests: only the HDF5 header
    is fetched (about 1 MB), never the data. The title says whether it's the final
    ("... Final product") or NRT ("... (nrt) product") version.
    """
    try:
        with (
            fsspec.open(
                JPL_BASE + FILE_STRING.format(date=day),
                headers=headers,
                # Read ahead in 1 MB blocks: the HDF5 header is many small reads, and
                # this fetches it in a few requests (~2-4 s) instead of many (~100 s)
                block_size=2**20,
                cache_type="readahead",
            ) as f,
            h5netcdf.File(f, "r") as nc,
        ):
            return _attrs_as_str(nc.attrs)
    # Any failure (HTTP, auth, a corrupt header) is reported as title_unknown and
    # retried, never guessed, so a broad catch is safe here
    except Exception as e:
        LOGGER.warning(f"Couldn't read the NetCDF attributes for {day}: {e}")
        return None


def status_from_title(
    day: date, title: str | None, today: date | None = None
) -> str | None:
    """The STAC status of data made from a NetCDF with this title, or None if unknown.

    final for the final product. For NRT, nrt_only once the date is more than
    NRT_ONLY_AFTER old (no final was ever published), otherwise preliminary.
    """
    if not title:
        return None
    if "Final product" in title:
        return "final"
    if "near-real-time" in title:
        if (today or datetime.now(UTC).date()) - day > NRT_ONLY_AFTER:
            return "nrt_only"
        return "preliminary"
    return None


def get_source_published(day: date) -> datetime | None:
    """When PO.DAAC published the current MUR NetCDF: the archive's Last-Modified.

    A HEAD request, so nothing is downloaded and no Earthdata login is needed.
    """
    try:
        response = HTTP.head(
            JPL_BASE + FILE_STRING.format(date=day),
            allow_redirects=True,
            timeout=(CONNECT_TIMEOUT, 60),
        )
        response.raise_for_status()
        return parsedate_to_datetime(response.headers["Last-Modified"])
    # Reported as published_unknown and retried on resume, never guessed
    except Exception as e:
        LOGGER.warning(f"Couldn't read the NetCDF publication time for {day}: {e}")
        return None


def oldest_write(times: Mapping[str, datetime | None]) -> datetime | None:
    """The oldest write time of a date's COGs (by variable), or None if it has none.

    The oldest, not any one of them: if a re-COG stopped partway, some COGs are new
    and the rest still come from the older NetCDF, and the date must not look final.

    Only COGs that exist count: older NetCDFs have no sst_anomaly, so those dates
    only ever have 4 COGs. A date counts as missing (None) if it has no analysed_sst.
    """
    if times.get("analysed_sst") is None:
        return None
    return min(t for t in times.values() if t is not None)


def _write_time(path: Path | S3Path) -> datetime | None:
    if _is_s3_path(path):
        try:
            return _public_s3().head_object(Bucket=path.bucket, Key=path.key)[
                "LastModified"
            ]
        except ClientError:
            return None
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


def get_cog_write_times(
    output_location: Path | S3Path, date: datetime
) -> dict[str, datetime | None]:
    """Each of the date's COGs' write time, by variable (None if it doesn't exist)."""
    return {
        var: _write_time(get_output_path(output_location, date, f"_{var}.tif"))
        for var in VARIABLES
    }


def get_cogs_written(output_location: Path | S3Path, date: datetime) -> datetime | None:
    """When the date's COGs were written: the oldest of them (see oldest_write)."""
    return oldest_write(get_cog_write_times(output_location, date))


def stac_is_stale(
    cog_times: Mapping[str, datetime | None], stac_written: datetime | None
) -> bool:
    """Whether any COG was written after the STAC item, so the item doesn't describe it.

    The STAC item is always written after its COGs, so this only happens when a run
    stopped between the two (or another run wrote the COGs meanwhile). Patching the
    status onto that item would leave the new COGs without their checksums.
    """
    written = [t for t in cog_times.values() if t is not None]
    return stac_written is not None and bool(written) and max(written) > stac_written


def classify(
    day: date,
    cog_written: datetime | None,
    source_produced: datetime | None,
    source_published: Callable[[], datetime | None],
    source_title: Callable[[], str | None],
    today: date | None = None,
) -> tuple[str, str]:
    """Whether a date's COG was made from the preliminary (NRT) or final NetCDF.

    `source_published` and `source_title` return the current NetCDF's publication time
    (archive Last-Modified) and `title`. Each is a network request, so they're only
    called when the rules before them haven't decided.

    Returns (status, reason), each a fixed string per rule, checked in this order
    (D = granule date, C = COG written, S = NetCDF produced, P = NetCDF published,
    T = NetCDF title):

    | status            | reason                                           | when                   |
    |-------------------|--------------------------------------------------|------------------------|
    | no_cmr_granule    | No COG and no CMR granule                        | no C, no S             |
    | missing           | No COG                                           | no C                   |
    | no_cmr_record     | No CMR record to compare with                    | no S                   |
    | preliminary       | NetCDF regenerated after COG                     | S > C                  |
    | published_unknown | Couldn't read NetCDF publication time            | P unreadable           |
    | preliminary       | NetCDF published after COG                       | P > C                  |
    | too_close         | COG written within 1 h of NetCDF publication     | C < P + CLOSE_CALL     |
    | final             | COG written from final NetCDF                    | T has "Final product"  |
    | nrt_only          | Archived NetCDF is still NRT; no final published | T NRT, D > 50 days ago |
    | preliminary       | COG written from NRT NetCDF                      | T NRT, D recent        |
    | title_unknown     | Couldn't read NetCDF title                       | T unreadable           |
    | title_unknown     | Unrecognised NetCDF title                        | any other T            |

    Once C >= P + CLOSE_CALL, the NetCDF was published before the cogger downloaded
    it, so the COG was made from the current NetCDF and its title decides.
    """
    if cog_written is None:
        if source_produced is None:
            return "no_cmr_granule", REASON_NO_DATA
        return "missing", REASON_NO_COG
    if source_produced is None:
        return "no_cmr_record", REASON_NO_CMR
    if source_produced > cog_written:
        return "preliminary", REASON_REGENERATED
    published = source_published()
    if published is None:
        return "published_unknown", REASON_PUBLISHED_UNKNOWN
    if published > cog_written:
        return "preliminary", REASON_PUBLISHED_AFTER
    if cog_written - published < CLOSE_CALL:
        return "too_close", REASON_TOO_CLOSE
    # The COG was made from the current NetCDF, so it has that NetCDF's status
    title = source_title()
    if title is None:
        return "title_unknown", REASON_TITLE_UNREADABLE
    status = status_from_title(day, title, today)
    if status == "final":
        return "final", REASON_FINAL_SOURCE
    if status == "nrt_only":
        return "nrt_only", REASON_NRT_ONLY
    if status == "preliminary":
        return "preliminary", REASON_NRT_SOURCE
    return "title_unknown", REASON_TITLE_UNKNOWN


def action_for(status: str, reason: str) -> str:
    """What the Lambda does with a date's audit result.

    - set_status: the COG already matches the current NetCDF, so only the STAC item's
      status is updated. Re-COGing would produce the same data.
    - reprocess: the COG was made from an older NetCDF (or is missing), so re-COG.
    - retry: the status can't be determined yet, so fail and let SQS retry.
    - skip: nothing has been published for this date yet.
    """
    if status in ("final", "nrt_only"):
        return "set_status"
    if status == "preliminary":
        return "set_status" if reason == REASON_NRT_SOURCE else "reprocess"
    if status in ("too_close", "missing"):
        return "reprocess"
    if status == "no_cmr_granule":
        return "skip"
    return "retry"


def clear_cache(cache_dir: Path = CACHE_DIR) -> None:
    """Delete any cached MUR NetCDFs, including partial downloads.

    All of them, not just this date's: a Lambda container runs one conversion at a
    time, so anything else in the cache is left over from a failed run.
    """
    for path in cache_dir.glob(FILE_STRING.replace("{date:%Y%m%d}", "*") + "*"):
        path.unlink(missing_ok=True)


def convert_date(
    date: datetime,
    input_location: str,
    output_location: Path | S3Path,
    overwrite: bool,
    cache_local: bool,
    log: Logger = LOGGER,
) -> Item:
    """Convert a date's NetCDF to COGs and a STAC item recording its status.

    The status comes from the downloaded NetCDF's own title, so it's exact.
    """
    day = date.date()
    produced = published = None
    if input_location.upper() == "JPL":
        # Provenance of the archived file. Published is read before the download
        try:
            produced = get_production_times(day, day).get(day)
        except GHRSSTException as e:
            log.warning(f"No CMR production time for {day}: {e}")
        published = get_source_published(day)

    if cache_local:
        # Leftovers from runs that failed or timed out on this container, which would
        # otherwise fill /tmp after a few failures
        clear_cache()
    try:
        input_path = get_input_path(input_location, date)
        log.info(f"Loading data from {input_path}")
        data = load_data(date, input_location, cache_local=cache_local, log=log)
        attrs = _attrs_as_str(data.attrs)
        status = status_from_title(day, attrs.get("title"))
        if status is None:
            raise GHRSSTException(
                f"Unrecognised NetCDF title for {day}: {attrs.get('title')!r}"
            )
        log.info(f"Source NetCDF is {status} ({attrs.get('title')})")

        log.info("Processing data...")
        processed = process_data(data)

        if _is_s3_path(output_location):
            log.info(f"Writing data to s3:/{output_location}")
        else:
            log.info(f"Writing data to {output_location}")
        written_files = write_data(processed, date, output_location, overwrite, log=log)

        log.info("Writing STAC")
        provenance = source_provenance(produced, published, attrs, converting=True)
        stac_doc = write_stac(
            processed, written_files, date, output_location, status, provenance, log=log
        )
    finally:
        if cache_local:
            clear_cache()

    log.info(f"Finished writing to: {stac_doc.self_href}")
    return stac_doc


def process_date(
    date: datetime,
    input_location: str,
    output_location: Path | S3Path,
    overwrite: bool = False,
    cache_local: bool = False,
    log: Logger = LOGGER,
    audit: bool = False,
) -> tuple[str, str] | None:
    """Process a date from a data source and output to a location

    Converts dates with no STAC item. For dates that have one, records or finalises
    their status: if the STAC item already says final it stops there. Otherwise it
    classifies the existing COG (the audit's rules) and either just updates the
    STAC item's status, or re-COGs if the COG was made from an older NetCDF (or is
    newer than the STAC item, so a run stopped before writing it).

    Args:
        date (datetime): Date to process
        input_location (str): Either 'jpl' to grab data from JPL, or a local folder to find the data in
        output_location (str): Location to output results to
        overwrite (bool): Re-COG even if the date is already done
        audit (bool): Only classify the existing COG as preliminary or final and return
            (status, reason). Never downloads or writes anything.
    """
    day = date.date()
    # Lookups shared by classify and the provenance, each fetched at most once
    cache: dict[str, Any] = {}

    def published() -> datetime | None:
        if "published" not in cache:
            cache["published"] = get_source_published(day)
        return cache["published"]

    def attrs() -> dict[str, str] | None:
        if "attrs" not in cache:
            # Earthdata login only if the NetCDF attributes are needed
            cache["attrs"] = get_source_attrs(day, get_headers())
        return cache["attrs"]

    def title() -> str | None:
        return (attrs() or {}).get("title")

    if audit:
        produced = get_production_times(day, day).get(day)
        status, reason = classify(
            day, get_cogs_written(output_location, date), produced, published, title
        )
        log.info(f"Audit {date:%Y-%m-%d}: {status} ({reason})")
        return status, reason

    log.info(
        f"Processing date {date:%Y-%m-%d} from {input_location} to {output_location}"
    )
    log.info(f"Overwrite: {overwrite}, Cache Local: {cache_local}")

    # Switch up our environment, in case we need to work on source.coop
    context = get_context()

    with environ(context):
        stac_file = get_output_path(output_location, date, ".stac-item.json")
        stac = None if overwrite else _read_json(stac_file)

        if stac is None:
            # Always rewrite the COGs: any left by an interrupted run might come from
            # an older NetCDF than the one whose status the new STAC item records
            convert_date(date, input_location, output_location, True, cache_local, log)
            return None

        current = stac.get("properties", {}).get(STATUS_PROPERTY)
        if current == "final":
            log.info(f"Skipping {date:%Y-%m-%d}: already final")
            return None

        produced = get_production_times(day, day).get(day)
        cog_times = get_cog_write_times(output_location, date)
        status, reason = classify(
            day, oldest_write(cog_times), produced, published, title
        )
        action = action_for(status, reason)
        log.info(f"{date:%Y-%m-%d}: {status} ({reason}) -> {action}")

        if action == "set_status" and stac_is_stale(cog_times, _write_time(stac_file)):
            log.info(
                "COGs are newer than the STAC item, so re-COG rather than patch it"
            )
            action = "reprocess"

        if action == "skip":
            return None
        if action == "retry":
            # Raising makes SQS retry the message, then send it to the dead-letter queue
            raise GHRSSTException(
                f"Can't determine the status of {date:%Y-%m-%d} yet: {status} ({reason})"
            )
        if action == "set_status":
            if status == current:
                log.info(f"STAC item already says {status}")
                return None
            provenance = source_provenance(
                produced, published(), attrs(), converting=False
            )
            set_stac_status(stac_file, status, provenance)
            log.info(f"Set {stac_file} status to {status}")
            return None

        # reprocess: the COG was made from an older NetCDF than the current one
        convert_date(date, input_location, output_location, True, cache_local, log)
    return None


def is_published(day: date) -> bool:
    """Whether CMR has a granule for the date. Raises if CMR can't be reached."""
    return day in get_production_times(day, day)


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
            if is_published(date.date()):
                # It should exist (e.g. a transient error mid-republication), so fail
                # and let SQS retry, then dead-letter, instead of dropping the date
                raise GHRSSTException(
                    f"NetCDF for {date:%Y-%m-%d} is in CMR but couldn't be read: {e}"
                ) from e
            # Not published yet (e.g. today): nothing to do until it is
            log.info(f"No NetCDF published yet for {date:%Y-%m-%d}")
    else:
        raise GHRSSTException("No date found in event, exiting")


@click.option("--date", type=str)
@click.option("--output-location", type=str)
@click.option("--input-location", type=str, default="JPL")
@click.option("--overwrite/--no-overwrite", is_flag=True, default=False)
@click.option("--cache-local/--no-cache-local", is_flag=True, default=False)
@click.option(
    "--audit/--no-audit",
    is_flag=True,
    default=False,
    help="Only report whether the existing COG is preliminary or final",
)
@click.command("ghrsst-cogger")
def main(date, output_location, input_location, overwrite, cache_local, audit):
    date = datetime.strptime(date, "%Y-%m-%d")

    if output_location.startswith("s3://"):
        output_location = S3Path(output_location.replace("s3://", "/"))
    else:
        output_location = Path(output_location)

    # Only catch known exceptions, and otherwise let the program crash
    try:
        process_date(
            date,
            input_location,
            output_location,
            overwrite,
            cache_local=cache_local,
            audit=audit,
        )
    except GHRSSTException as e:
        print(f"Failed to process date {date:%Y-%m-%d} with error {e}")
        exit(1)
    except FileNotFoundError as e:
        print(f"Couldn't find file for date {date:%Y-%m-%d} from {e}")


if __name__ == "__main__":
    main()
