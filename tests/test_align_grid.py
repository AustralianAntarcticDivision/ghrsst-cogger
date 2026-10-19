from datetime import datetime
from pathlib import Path

import pytest

from ghrsst.cogger import load_data, process_data

LOCAL_FOLDER = "data"
LOCAL_FILE_DATE = datetime(2023, 11, 6)
ROOT_FOLDER = Path(__file__).parent.parent

AFFINE_BEFORE = [
    0.009999999728725104,
    0.0,
    -179.99500549302843,
    0.0,
    0.009999999762614682,
    -89.99499786365084,
    0.0,
    0.0,
    1.0,
]

AFFINE_AFTER = [0.01, 0.0, -180.0, 0.0, 0.01, -89.995, 0.0, 0.0, 1.0]


# Need to skip this test in CI because the file is large
@pytest.fixture
def xarray_sample():
    if not any((ROOT_FOLDER / LOCAL_FOLDER).glob(f"{LOCAL_FILE_DATE:%Y%m%d}*.nc")):
        pytest.skip("Sample NetCDF not in data/, see Makefile")
    data =load_data(LOCAL_FILE_DATE, str(ROOT_FOLDER / LOCAL_FOLDER))

    return data


def test_load_data(xarray_sample):
    assert xarray_sample is not None


def test_process_data(xarray_sample):
    affine = list(xarray_sample.odc.transform)

    assert affine == AFFINE_BEFORE

    processed = process_data(xarray_sample)
    affine_after = list(processed.odc.transform)

    print(affine_after)
    print(processed.lat.values[0], processed.lat.values[-1])

    assert affine_after == AFFINE_AFTER
