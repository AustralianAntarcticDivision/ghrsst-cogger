---
title: How it's made
nav_order: 8
---

# How it's made
{: .no_toc }

A summary of the conversion and how it keeps the data final. The
[repository README](https://github.com/AustralianAntarcticDivision/ghrsst-cogger#readme)
has the details for running and deploying it.

1. TOC
{:toc}

## The conversion

For each date, [`ghrsst/cogger.py`](https://github.com/AustralianAntarcticDivision/ghrsst-cogger/blob/main/ghrsst/cogger.py):

1. Downloads the day's MUR NetCDF from PO.DAAC, with an Earthdata login.
2. Opens it with xarray, without applying scale and offset, so the integers are kept.
3. Flips latitude to north-up and sets the exact geotransform
   (`-179.995, 0.01, 0, 89.995, 0, -0.01`) in `EPSG:4326`.
4. Writes one COG per variable (512-pixel tiles, ZSTD, five overview levels), with the
   scale, offset and nodata in each COG's metadata.
5. Writes a STAC item for the day, recording which version of the source it came from,
   its provenance, and each COG's checksum and size.

Every day, a GeoParquet index of all the STAC items is rebuilt by
[`ghrsst/create_parquet.py`](https://github.com/AustralianAntarcticDivision/ghrsst-cogger/blob/main/ghrsst/create_parquet.py).

## The infrastructure

![Architecture](https://raw.githubusercontent.com/AustralianAntarcticDivision/ghrsst-cogger/main/architecture.png)

It runs on AWS in `us-west-2`, next to the Source Cooperative bucket, as three Lambda
functions sharing one container image, defined with Terraform:

| Function | When | What |
|---|---|---|
| `ghrsst-lambda-daily` | 00:00 UTC every day | Queues the last 52 days that aren't already `final` |
| `ghrsst-lambda` | For each queued date | Converts the date, or records or finalises its status |
| `ghrsst-lambda-parquet-daily` | 01:00 UTC every day | Rebuilds `ghrsst-mur-v2.parquet` |

The dates go through an SQS queue, with a dead-letter queue for dates that keep
failing, and at most 15 conversions run at once.

## Preliminary and final in the pipeline

Each source file is replaced in place when its final version is published (see
[Preliminary and final](data.md#preliminary-and-final)), so the pipeline revisits each
day until it's final. For a queued date, `ghrsst-lambda`:

- **Has no STAC item:** converts it. The status comes from the downloaded NetCDF's own
  title.
- **Has a STAC item that says `final`:** stops.
- **Otherwise:** compares the COGs' write time with when the source was produced
  (from NASA's CMR catalogue) and published (from the archive), and reads the source
  file's title:
  - If the COGs were made from the current source, it updates only the STAC item's
    status.
  - If the source has been replaced since the COGs were made, it re-converts the day.
  - If it can't tell yet, it fails, and the queue retries the date later.

The same rules, run read-only over every date, are the audit (`make audit`) that found
and measured the preliminary days in October 2026 (see
[Background](background.md#preliminary-data-and-the-audit)).

## Releases and changes

The code is on [GitHub](https://github.com/AustralianAntarcticDivision/ghrsst-cogger).
Each release is a tagged container image, and each STAC item's `processing:software`
records the versions that made its COGs.
