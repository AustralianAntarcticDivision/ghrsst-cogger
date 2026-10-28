from ghrsst.cogger import DROP_VARIABLES, SEA_ICE_UNIT, VARIABLES, process_data

# The published grid: 0.01 degree pixels, north-up, origin at the top-left pixel corner
AFFINE_AFTER = [0.01, 0.0, -179.995, 0.0, -0.01, 89.995, 0.0, 0.0, 1.0]


def test_load_data(sample):
    # Raw stored values (no scale/offset or masking), without the dropped variables
    assert list(sample.data_vars) == VARIABLES
    assert not set(DROP_VARIABLES) & set(sample.data_vars)
    assert str(sample["analysed_sst"].dtype) == "int16"
    assert "scale_factor" in sample["analysed_sst"].attrs
    assert dict(sample.sizes) == {"time": 1, "lat": 17999, "lon": 36000}
    assert "Final product" in sample.attrs["title"]


def test_process_data(sample):
    processed = process_data(sample)

    assert list(processed.odc.transform) == AFFINE_AFTER
    # Flipped to north-up, and SST metadata converted to celsius
    assert processed.lat.values[0] > processed.lat.values[-1]
    assert processed["analysed_sst"].attrs["units"] == "celsius"
    assert processed["analysed_sst"].attrs["add_offset"] == 25
    # The sample (2023) source has no sea_ice_fraction unit, so it gets the older one
    assert processed["sea_ice_fraction"].attrs["units"] == SEA_ICE_UNIT
