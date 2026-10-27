# GHRSST Cogger and Lambda Function

Documentation for using the data, with background and examples for Python, R, GDAL,
DuckDB and the browser: <https://australianantarcticdivision.github.io/ghrsst-cogger/>
(source in [`docs/`](docs/)). This README covers developing and running the pipeline.

## Overview

The architecture for this process has the following components:

* The code in this repository, including the [COG creation](ghrsst/cogger.py),
  [date seeding](ghrsst/dategen.py) for the SQS queue and the
  [GeoParquet index](ghrsst/create_parquet.py)
* A Docker image, with a [Dockerfile](Dockerfile), used by all three Lambdas
* Terraform infrastructure as code, which includes:
  * An SQS queue to hold tasks
  * An SQS dead-letter queue to handle failed tasks
  * `ghrsst-lambda`: for the date in each queue message, converts it to COGs and a
    STAC item on source.coop, or records/finalises the status of an existing one
    (see [Finalising preliminary COGs](#finalising-preliminary-cogs))
  * `ghrsst-lambda-daily`: runs every day at 00:00 UTC (CloudWatch schedule) and
    queues the last 52 days, skipping dates that are already final, so new dates are
    converted and preliminary ones are finalised when the final NetCDF lands
  * `ghrsst-lambda-parquet-daily`: runs every day at 01:00 UTC and rebuilds
    `ghrsst-mur-v2.parquet`, a STAC GeoParquet index of every date from 2002-06-01
  * A GitHub OIDC role that lets the release workflow push images to ECR

![Architecture](architecture.png).

## Quickstart

Dependencies are managed with [uv](https://docs.astral.sh/uv/). Python 3.12 is pinned in
`.python-version` and uv will download it if needed.

```bash
uv sync
source .venv/bin/activate
prek install  # run ruff, ty and file checks on each commit

make test       # tests that need the sample NetCDF in data/ are skipped without it
make test-network  # also against the real CMR, PO.DAAC and source.coop (EARTHDATA_* for some)
make lint       # all prek hooks on all files: ruff, ty, uv lock --check
make run-local  # convert one date from data/ to data/output
```

The tests use a sample MUR NetCDF (~700 MB) in `data/`. Download it with `make download-test-data`,
which needs an [Earthdata login](https://urs.earthdata.nasa.gov/users/new) in
`EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD` (or a token in `EARTHDATA_TOKEN`).

### Auditing preliminary vs final COGs

`make audit` classifies every date's COG on source.coop as `preliminary`, `final` or
another status (see `classify()` in `ghrsst/cogger.py`) and writes `data/audit.csv`.
It's read-only: it never downloads a NetCDF or writes a COG.

```bash
export EARTHDATA_USERNAME=... EARTHDATA_PASSWORD=...  # to read the NetCDF titles
caffeinate -is make audit 2>&1 | tee -a data/audit.log  # caffeinate keeps a Mac awake
```

It can take a few hours. Rows are saved as they go, so if it stops, run the same command
again to resume.

**Why an audit is needed.** MUR publishes each date's NetCDF twice, replacing it in
place under the same name:

- **NRT:** about 1–2 days after the date (`title`: "Daily MUR SST, Interim
  near-real-time (nrt) product").
- **Final:** later (`title`: "Daily MUR SST, Final product"). It's usually 9 days after
  the date from 2020 onwards, and about 4 days in 2016. Occasionally it's much later
  (up to ~40 days). Dates up to 2015 were all reprocessed in 2015.

The COGs on source.coop were made in three ways:

| When (UTC) | Dates | Source NetCDF used |
|---|---|---|
| Bulk run, 2025-03-20 07:14–15:02 | 2002-06-01 → 2024-12-31 | Final (long since published), apart from ~50 dates whose archived NetCDF is still NRT |
| Bulk run, 2025-02-11 02:31 | 2025-01-01 → 2025-02-09 | Final up to ~2025-02-01, NRT after: the finals for later dates didn't exist yet |
| Daily Lambda, from 2025-02-11 23:56 | 2025-02-10 onwards | NRT: each date was COGed 1–4 days after it (mostly 2) |

The daily job skips any date that already has a STAC item, so it never revisited a date
once its final NetCDF was published. Every daily-ingested COG is therefore preliminary
until it's reprocessed. The audit measures exactly which dates are affected, and the
finalise process reprocesses `preliminary` and `too_close` dates.

To audit one date:

```bash
python ghrsst/cogger.py --date YYYY-MM-DD --audit \
  --output-location s3://us-west-2.opendata.source.coop/ausantarctic/ghrsst-mur-v2/
```

### Finalising preliminary COGs

Each STAC item records whether its COGs came from MUR's NRT or final NetCDF:
- `ghrsst:status`: `final`, `preliminary` or `nrt_only`
- provenance in the [Processing extension](https://github.com/stac-extensions/processing) (`processing:*`), plus `ghrsst:source_published`
- a `derived_from` link to the source NetCDF

When converting, the status comes from the downloaded NetCDF's own title.

Every day, `ghrsst-lambda-daily` queues the last 52 days, skipping dates whose STAC item already says `final` (read anonymously). For each queued date, `ghrsst-lambda`:

- **Has no STAC item:** converts it.
- **STAC item already says `final`:** stops (one S3 read).
- **Otherwise:** classifies the existing COGs with the audit's rules (`classify()`), using the **oldest** write time of the date's COGs (5, or 4 for older dates without `sst_anomaly`), so a re-COG that stopped partway is redone rather than looking final. Then acts on the result (`action_for()`):
  - The COG matches the current NetCDF (`final`, `nrt_only`, or NRT with the final not out yet) → updates only the STAC item's status.
  - The COG was made from an older NetCDF (`preliminary`, `too_close`) → re-COGs it.
  - The status can't be determined yet → fails, so SQS retries, then moves the message to the dead-letter queue.

To backfill every date, with your AWS credentials for the IDEA_AAD account:

```bash
python -m ghrsst.dategen --start 2002-06-01   # optional --end YYYY-MM-DD; --include-final to re-check final dates too
```

The queue runs at most 15 Lambdas at once (`maximum_concurrency`) and keeps messages for
4 days; failed messages stay in the dead-letter queue for 14 days. Each Lambda can run
for up to 15 minutes.

A date fails, and is retried, when its status can't be determined yet (e.g. CMR is missing
a record), or when its NetCDF is in CMR but can't be downloaded. A date whose NetCDF isn't
published yet (e.g. today) is just skipped. After 3 failed attempts a message moves to the
dead-letter queue. Once the cause has cleared, move those messages back to the main queue:

```bash
aws sqs get-queue-attributes --attribute-names ApproximateNumberOfMessages \
  --queue-url "$(aws sqs get-queue-url --queue-name ghrsst-queue-dead --query QueueUrl --output text)"
aws sqs start-message-move-task \
  --source-arn arn:aws:sqs:us-west-2:381491825451:ghrsst-queue-dead \
  --destination-arn arn:aws:sqs:us-west-2:381491825451:ghrsst-queue
```

Re-made COGs keep their URLs, so their byte offsets change. Each re-made asset records
`file:checksum` and `file:size`, and its item has `updated`, so anyone holding byte-range
references (kerchunk, virtual Zarr) can detect the change.

Add a dependency with `uv add <package>` (or `uv add --dev <package>`), which updates
`pyproject.toml` and `uv.lock`. Commit both.

Build the Lambda image with `make build VERSION=x.y.z` and run the tests inside the image
with `make test-docker`.

## Cost calculation

Lambda (x86) costs `$0.0000166667` per GB-second, after a free tier of 400k GB-s a month.
`ghrsst-lambda` runs with `10 GB` of memory, so a ~3–5 minute conversion is about $0.03–0.05.

- **Finalise backfill (one-off):** about 1.9–2.3M GB-s, so about **$25–32** once the free
  tier is used up:
  - about 623 re-COGs (~1.1M GB-s)
  - about 8,280 status-only updates at ~10–15 s each (~0.8–1.2M GB-s)

  Splitting it across two calendar months saves ~$7.
- **Daily:** about 2 conversions a day (the newest date, and the one whose final just
  landed). Dates already final aren't queued, so this stays within the free tier.

## AWS access

The infrastructure is in the `IDEA_AAD` account (`381491825451`), region `us-west-2`.
Sign in as your IAM user with [`aws login`](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html),
which reuses your console sign-in so there are no access keys to store. It needs AWS
CLI 2.32 or later.

```bash
aws login --profile aad    # opens the browser; choose us-west-2 the first time
aws sts get-caller-identity --profile aad    # arn:aws:iam::381491825451:user/<you>
```

Terraform can't read `aws login` credentials yet, so export temporary keys into the
shell before running `terraform` or `make plan`/`make apply`:

```bash
eval "$(aws configure export-credentials --profile aad --format env)"
export AWS_REGION=us-west-2
make init plan
```

The exported keys expire with the session, so re-run the `eval` line after logging in again.
The AWS CLI and `make get-ecr-login` can use the profile directly (`export AWS_PROFILE=aad`).

## Infra deployment

Create secrets on AWS for the Earthdata username and password.

```bash
aws secretsmanager create-secret \
    --name earthdata-username \
    --secret-string secretusername \
    --region us-west-2
```

```bash
aws secretsmanager create-secret \
    --name earthdata-password \
    --secret-string secretpassword \
    --region us-west-2
```

Create secrets for AWS Access and Secret key for the prod bucket. (These are optional:
`ghrsst-lambda` writes to source.coop with its IAM role, `ghrsst-data-writer-role`, which
the bucket policy in [source-coop-policy.json](source-coop-policy.json) grants access to.
The key-based `SOURCECOOP_*` variables are commented out in `terraform/main.tf`.)

```bash
aws secretsmanager create-secret \
    --name source-coop-access-key \
    --secret-string accesskey \
    --region us-west-2
```

```bash
aws secretsmanager create-secret \
    --name source-coop-secret-key \
    --secret-string secretaccesskey \
    --region us-west-2
```

Manually create a bucket that will be used for [storing Terraform state](https://developer.hashicorp.com/terraform/language/backend/s3).

```bash
aws s3 mb s3://aad-ghrsst-terraform-state
```

Configure terraform bucket and path in the top of the [terraform/main.tf](terraform/main.tf) file.

Initialise, plan and apply using the terraform command line:

* `terraform init`
* `terraform plan`
* `terraform apply`.

This will likely fail on the Lambda step, so
[push the Docker image](https://docs.aws.amazon.com/AmazonECR/latest/userguide/docker-push-ecr-image.html).

```bash
make get-ecr-login build push VERSION=x.y.z
```

Now apply again: `terraform apply`.


## Releasing

GitHub Actions runs **PR - Lint, build and test** (`.github/workflows/pr-lint-build-test.yml`)
on every PR and push to `main`. Pushing a version tag runs **Release - push image to ECR**
(`release-push-image.yml`), which builds the Lambda image and pushes it to ECR,
authenticating with GitHub OIDC (the `ghrsst-github-actions` role in Terraform).

1. Set `version` in `pyproject.toml` (e.g. `0.13.0`) and merge to `main`.
2. Tag and push: `git tag v0.13.0 && git push upstream v0.13.0`. A pre-release tag such as
   `v0.13.0-rc1` pushes `:0.13.0-rc1` without moving `:latest`. The tag must match the
   `pyproject.toml` version.
3. When the workflow has finished, set `image_tag` in `terraform/prod.tfvars` and run
   `make plan`, then `make apply` (see [AWS access](#aws-access)). Deploys are manual.
