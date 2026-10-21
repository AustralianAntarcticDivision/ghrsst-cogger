#!/usr/bin/env python3

import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import boto3
import click
from s3path import S3Path

from ghrsst.cogger import STATUS_PROPERTY, _public_s3, get_logger, get_output_path

# Finals usually land 9 days after the date, occasionally up to ~40, and a date becomes
# nrt_only after 50 days (NRT_ONLY_AFTER), so the look-back must be longer than that.
N_PREVIOUS_DAYS = int(os.environ.get("N_PREVIOUS_DAYS", "52"))
QUEUE_NAME = os.environ.get("QUEUE_NAME", "ghrsst-queue")
OUTPUT_LOCATION = os.environ.get(
    "OUTPUT_LOCATION",
    "s3://us-west-2.opendata.source.coop/ausantarctic/ghrsst-mur-v2/",
)
# SQS accepts at most 10 messages per send_messages call
SQS_BATCH_SIZE = 10
LOGGER = get_logger(__name__)


def is_final(location: S3Path, date: datetime) -> bool:
    """Whether the date's STAC item already says final.

    Read anonymously (the data is public), so no extra IAM permission is needed. Any
    failure counts as not final, so a failed check queues the date rather than drop it.
    """
    stac_file = get_output_path(location, date, ".stac-item.json")
    try:
        body = _public_s3().get_object(Bucket=stac_file.bucket, Key=stac_file.key)
        doc = json.loads(body["Body"].read())
    except Exception:
        return False
    return doc.get("properties", {}).get(STATUS_PROPERTY) == "final"


def dates_to_queue(
    dates: list[datetime],
    final: Callable[[datetime], bool],
    include_final: bool = False,
) -> list[datetime]:
    """The dates that still need the Lambda: everything that isn't already final.

    Only an optimisation: the Lambda also skips final dates, so a stale check is safe.
    """
    if include_final:
        return dates
    with ThreadPoolExecutor(max_workers=16) as pool:
        done = list(pool.map(final, dates))
    return [d for d, is_done in zip(dates, done, strict=True) if not is_done]


def queue_dates(dates: list[datetime], queue_name: str = QUEUE_NAME) -> int:
    """Send one {"date": "YYYY-MM-DD"} message per date to the queue."""
    sqs = boto3.resource("sqs", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    queue = sqs.get_queue_by_name(QueueName=queue_name)
    LOGGER.info(f"Sending {len(dates)} messages to {queue_name}")
    for i in range(0, len(dates), SQS_BATCH_SIZE):
        entries = [
            {
                "Id": f"{d:%Y-%m-%d}",
                "MessageBody": json.dumps({"date": f"{d:%Y-%m-%d}"}),
            }
            for d in dates[i : i + SQS_BATCH_SIZE]
        ]
        response = queue.send_messages(Entries=entries)
        if response.get("Failed"):
            raise RuntimeError(f"SQS rejected messages: {response['Failed']}")
    return len(dates)


def _location(output_location: str) -> S3Path:
    return S3Path(output_location.replace("s3://", "/"))


def lambda_handler(event, _):
    log = LOGGER
    # UTC, matching MUR granule dates, whatever the local clock is
    today = datetime.now(UTC).replace(tzinfo=None)
    log.info(f"Event: {event}")
    log.info(f"Working on date: {today:%Y-%m-%d}")
    location = _location(OUTPUT_LOCATION)
    dates = [today - timedelta(days=i) for i in range(N_PREVIOUS_DAYS)]
    todo = dates_to_queue(dates, lambda d: is_final(location, d))
    log.info(f"{len(dates) - len(todo)} of {len(dates)} dates already final")
    if todo:
        queue_dates(todo)


@click.command("ghrsst-dategen")
@click.option("--start", type=str, required=True, help="First date (YYYY-MM-DD)")
@click.option(
    "--end", type=str, default=None, help="Last date (YYYY-MM-DD), default today UTC"
)
@click.option("--queue-name", type=str, default=QUEUE_NAME, show_default=True)
@click.option("--output-location", type=str, default=OUTPUT_LOCATION, show_default=True)
@click.option(
    "--include-final/--skip-final",
    default=False,
    help="Also queue dates whose STAC item already says final (default: skip them)",
)
def main(start, end, queue_name, output_location, include_final):
    """Queue every date from --start to --end, e.g. to backfill STAC statuses."""
    start = datetime.strptime(start, "%Y-%m-%d")
    end = (
        datetime.strptime(end, "%Y-%m-%d")
        if end
        else datetime.now(UTC).replace(tzinfo=None)
    )
    dates = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    location = _location(output_location)
    todo = dates_to_queue(dates, lambda d: is_final(location, d), include_final)
    click.echo(f"{len(dates) - len(todo)} of {len(dates)} dates already final")
    if todo:
        click.echo(f"Queued {queue_dates(todo, queue_name)} dates to {queue_name}")


if __name__ == "__main__":
    main()
