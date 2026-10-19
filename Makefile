# TERRAFORM STUFF
init:
	terraform -chdir=terraform init

plan:
	terraform -chdir=terraform plan \
		-var-file=prod.tfvars

apply:
	terraform -chdir=terraform apply \
		-var-file=prod.tfvars

destroy:
	terraform -chdir=terraform destroy \
		-var-file=prod.tfvars


# SOURCE COOP STUFF
update-readme:
	AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED \
		aws s3 cp readme_for_source_coop.md s3://ausantarctic/ghrsst-mur-v2/README.md

count-stac:
	aws s3 ls --recursive s3://ausantarctic/ghrsst-mur-v2/ | grep '\.json$' | wc -

# DOCKER STUFF
get-ecr-login:
	aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin 381491825451.dkr.ecr.us-west-2.amazonaws.com

# Run with `make build VERSION=0.1.0`
build:
	docker buildx build --platform linux/amd64 --target final -t ghrsst-cogger:latest .
	docker tag ghrsst-cogger:latest 381491825451.dkr.ecr.us-west-2.amazonaws.com/ghrsst-cogger:latest
	docker tag ghrsst-cogger:latest 381491825451.dkr.ecr.us-west-2.amazonaws.com/ghrsst-cogger:$(VERSION)

push:
	docker push 381491825451.dkr.ecr.us-west-2.amazonaws.com/ghrsst-cogger:latest
	docker push 381491825451.dkr.ecr.us-west-2.amazonaws.com/ghrsst-cogger:$(VERSION)

test-docker:
	docker buildx build --platform linux/amd64 --target test -t ghrsst-cogger:test .
	docker run --rm -v $(PWD)/data:/code/data ghrsst-cogger:test

# CODE TESTING STUFF
# Sample NetCDF for the tests (~700 MB). Needs an Earthdata login, see README
download-test-data: data/20231106090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.nc

test:
	uv run pytest tests/

# Count preliminary vs final COGs on source.coop. Read-only, writes data/audit.csv
audit:
	uv run python -m ghrsst.audit

lint:
	uv run prek run --all-files

format:
	uv run ruff check --fix .
	uv run ruff format .

data/20231106090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.nc:
	uv run python -c "import earthaccess; earthaccess.login(); earthaccess.download(['https://archive.podaac.earthdata.nasa.gov/podaac-ops-cumulus-protected/MUR-JPL-L4-GLOB-v4.1/$(notdir $@)'], 'data')"

run-source-coop:
	python3 ghrsst/cogger.py \
		--date "2025-01-01" \
		--input-location data \
		--output-location "s3://ausantarctic/ghrsst-mur-v2/" \
		--overwrite

run-local:
	python3 ghrsst/cogger.py \
		--date "2025-02-02" \
		--input-location data \
		--output-location data/output \
		--overwrite

run-s3: data/20231106090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.nc
	python3 ghrsst/cogger.py \
		--date "2025-01-01" \
		--input-location data \
		--output-location s3://idea-ghrsst-testing/ghrsst-mur-v2/ \
		--cache-local \
		--overwrite

run-dl: data/20231106090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.nc
	python3 ghrsst/cogger.py \
		--date "2024-04-12" \
		--input-location "JPL" \
		--output-location data/output \
		--overwrite

run-dl-cache: data/20231106090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.nc
	python3 ghrsst/cogger.py \
		--date "2024-04-12" \
		--input-location "JPL" \
		--output-location data/output \
		--overwrite \
		--cache-local
