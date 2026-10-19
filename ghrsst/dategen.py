#!/usr/bin/env python3

import json
import os
from datetime import datetime, timedelta

import boto3

from ghrsst.cogger import get_logger

N_PREVIOUS_DAYS = int(os.environ.get("N_PREVIOUS_DAYS", "7"))
QUEUE_NAME = os.environ.get("QUEUE_NAME", "ghrsst-queue")
LOGGER = get_logger(__name__)


def lambda_handler(event, _):
    log = LOGGER
    today = datetime.today()
    log.info(f"Event: {event}")
    log.info(f"Working on date: {today:%Y-%m-%d}")

    messages = []

    for i in range(N_PREVIOUS_DAYS):
        dt = today - timedelta(days=i)

        body = {
            "date": dt.strftime("%Y-%m-%d"),
        }
        messages.append({"Id": body["date"], "MessageBody": json.dumps(body)})

    log.info(f"Getting queue: {QUEUE_NAME}")
    sqs = boto3.resource("sqs")
    queue = sqs.get_queue_by_name(QueueName=QUEUE_NAME)

    log.info(f"Sending {len(messages)} messages")
    queue.send_messages(Entries=messages)
