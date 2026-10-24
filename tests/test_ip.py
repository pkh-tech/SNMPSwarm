import pytest

from src.ip import calculate_ip


@pytest.mark.parametrize(
    ("device_id", "expected"),
    [
        (1, "10.0.0.1"),
        (23, "10.0.0.23"),
        (99, "10.0.0.99"),
        (100, "10.0.1.0"),
        (167, "10.0.1.67"),
        (200, "10.0.2.0"),
    ],
)
def test_device_ip_mapping(device_id, expected):
    assert calculate_ip(device_id) == expected


def test_device_id_must_be_positive():
    with pytest.raises(ValueError):
        calculate_ip(0)
