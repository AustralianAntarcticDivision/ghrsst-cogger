from datetime import datetime
from pathlib import Path

import pytest

from ghrsst.cogger import load_data, process_data

LOCAL_FOLDER = "data"
LOCAL_FILE_DATE = datetime(2023, 11, 6)
ROOT_FOLDER = Path(__file__).parent.parent

# The published grid: 0.01 degree pixels, north-up, origin at the top-left pixel corner
AFFINE_AFTER = [0.01, 0.0, -179.995, 0.0, -0.01, 89.995, 0.0, 0.0, 1.0]


# Need to skip this test in CI because the file is large
# TODO: use scope="module" if more tests are added, so the file is opened once
@pytest.fixture
def xarray_sample():
    if not any((ROOT_FOLDER / LOCAL_FOLDER).glob(f"{LOCAL_FILE_DATE:%Y%m%d}*.nc")):
        pytest.skip("Sample NetCDF not in data/, run `make download-test-data`")
    data = load_data(LOCAL_FILE_DATE, str(ROOT_FOLDER / LOCAL_FOLDER))

    return data


def test_load_data(xarray_sample):
    assert xarray_sample is not None


def test_process_data(xarray_sample):
    processed = process_data(xarray_sample)

    assert list(processed.odc.transform) == AFFINE_AFTER
