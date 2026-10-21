from datetime import datetime
from pathlib import Path

import pytest

from ghrsst.cogger import load_data

SAMPLE_DATE = datetime(2023, 11, 6)
DATA_FOLDER = Path(__file__).parent.parent / "data"


@pytest.fixture(scope="session")
def sample():
    """The real sample MUR NetCDF (final product), opened once for the whole session.

    Skipped without the file, which is large and needs an Earthdata login.
    """
    if not any(DATA_FOLDER.glob(f"{SAMPLE_DATE:%Y%m%d}*.nc")):
        pytest.skip("Sample NetCDF not in data/, run `make download-test-data`")
    return load_data(SAMPLE_DATE, str(DATA_FOLDER))
