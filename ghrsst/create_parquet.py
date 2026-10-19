import asyncio
import os
from datetime import datetime, timedelta
from logging import Logger

import click
import stacrs
from s3path import S3Path

from ghrsst.cogger import GHRSSTException, get_logger, get_output_path

LOGGER = get_logger(__name__)


async def fetch_all_items(dates, input_location, concurrent_requests=20):
    location = S3Path(input_location)
    hrefs = [
        str(get_output_path(location, date, ".stac-item.json")).replace(
            input_location, "https://data.source.coop/ausantarctic/ghrsst-mur-v2"
        )
        for date in dates
    ]

    print(f"Prepared {len(hrefs)} hrefs")

    async def fetch_item(href, semaphore):
        async with semaphore:
            try:
                return await stacrs.read(href)
            except Exception as e:
                print(f"Failed to fetch {href} with error {e}")
                return None

    semaphore = asyncio.Semaphore(concurrent_requests)
    tasks = [asyncio.create_task(fetch_item(href, semaphore)) for href in hrefs]
    items = await asyncio.gather(*tasks)

    return [item for item in items if item is not None]


async def get_items(dates, input_location):
    location = S3Path(input_location)
    hrefs = []

    for date in dates:
        path = get_output_path(location, date, ".stac-item.json")
        href = str(path).replace(
            input_location, "https://data.source.coop/ausantarctic/ghrsst-mur-v2"
        )

        hrefs.append(href)

    print(f"Prepared {len(hrefs)} hrefs")

    async def fetch_item(href):
        try:
            item = await stacrs.read(href)
            return item
        except Exception as e:
            print(f"Failed to fetch {href} with error {e}")
            return None

    items = await asyncio.gather(*[fetch_item(href) for href in hrefs])
    items = [item for item in items if item is not None]

    return items


async def create_parquet(
    start_date: datetime,
    end_date: datetime,
    input_location: str,
    output_location: str,
    write_tempfile: bool = False,
    log: Logger = LOGGER,
) -> int:
    dates = [
        start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)
    ]

    log.info(f"Working from {start_date:%Y-%m-%d} to {end_date:%Y-%m-%d}")
    log.info(
        f"Reading from {input_location}, writing to {output_location}, write_tempfile is {write_tempfile}"
    )

    items = await fetch_all_items(dates, input_location)

    log.info(f"Found {len(items)} STAC items")

    # Write items to parquet
    output_location = output_location.replace("s3:/", "s3://")
    out = f"{output_location}/ghrsst-mur-v2.parquet"

    log.info(f"Writing {len(items)} items to {out}")

    if len(items) == 0:
        raise GHRSSTException("No items to write to parquet")

    if output_location.startswith("s3://"):
        if write_tempfile:
            # Write to a memoryfile
            temp_file = "/tmp/ghrsst-mur-v2.parquet"
            log.info(f"Writing to tempfile at: {temp_file}")
            await stacrs.write(temp_file, items)

            # Move the tempfile to the final location on S3
            out = (
                S3Path(output_location.replace("s3://", "/")) / "ghrsst-mur-v2.parquet"
            )

            log.info(f"Copying tempfile to S3 at : S3:/{out}")
            with open(temp_file, "rb") as f:
                out.write_bytes(f.read())
        else:
            # Write direct to S3
            log.info(f"Writing {len(items)} items to S3 at: {out}")
            await stacrs.write(out, items)
    else:
        await stacrs.write(out, items)

    return len(items)


def lambda_handler(event, _):
    log = LOGGER
    end_date = datetime.today()
    log.info(f"Event: {event}")
    log.info(f"Working on date: {end_date:%Y-%m-%d}")

    start_date_str = os.environ.get("START_DATE", "2025-01-01")
    start_date = datetime.strptime(start_date_str, "%Y-%m-%d")

    input_location = os.environ.get(
        "INPUT_LOCATION", "s3:/ausantarctic/ghrsst-mur-v2"
    ).replace("s3://", "s3:/")
    output_location = os.environ.get("OUTPUT_LOCATION", input_location).replace(
        "s3://", "s3:/"
    )
    write_tempfile = os.environ.get("WRITE_TEMPFILE", "true").lower() == "true"

    result = asyncio.run(
        create_parquet(
            start_date=start_date,
            end_date=end_date,
            input_location=input_location,
            output_location=output_location,
            write_tempfile=write_tempfile,
            log=log,
        )
    )

    log.info(f"Replaced parquet file with a new one with {result} items in it.")


@click.option("--start-date", type=str)
@click.option("--end-date", type=str)
@click.option("--input-location", type=str, default="s3://ausantarctic/ghrsst-mur-v2")
@click.option("--output-location", type=str, default=None)
@click.option("--write-tempfile/--no-write-tempfile", is_flag=True, default=True)
@click.command("create-parquet")
def main(start_date, end_date, input_location, output_location, write_tempfile):
    start_date = datetime.strptime(start_date, "%Y-%m-%d")
    end_date = datetime.strptime(end_date, "%Y-%m-%d")

    input_location = input_location.replace("s3://", "s3:/")

    if output_location is None:
        output_location = input_location
    else:
        output_location = output_location.replace("s3://", "s3:/").rstrip("/")

    # Only catch known exceptions, and otherwise let the program crash
    try:
        log = LOGGER
        result = asyncio.run(
            create_parquet(
                start_date=start_date,
                end_date=end_date,
                input_location=input_location,
                output_location=output_location,
                write_tempfile=write_tempfile,
                log=log,
            )
        )

        click.echo(
            f"Created parquet with {result} rows, written to {output_location.replace('s3:/', 's3://')}/ghrsst-mur-v2.parquet"
        )
    except GHRSSTException as e:
        print(f"Failed to create parquet with error {e}")
        exit(1)


if __name__ == "__main__":
    main()
