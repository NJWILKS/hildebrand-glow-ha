from custom_components.hildebrand_glow.identity import (
    config_unique_id,
    sensor_unique_id,
    site_identity,
)


def test_site_identity_prefers_bright_virtual_entity() -> None:
    assert site_identity("bright-site-123", "entry-a") == "bright-site-123"
    assert site_identity("bright-site-123", "entry-b") == "bright-site-123"


def test_site_identity_falls_back_for_legacy_entries() -> None:
    assert site_identity(None, "entry-a") == "entry-a"


def test_config_entry_identity_is_account_and_site_specific() -> None:
    assert config_unique_id(" Nick@Example.COM ", "site-1") == "nick@example.com:site-1"
    assert config_unique_id("Nick@example.com", "site-2") == "nick@example.com:site-2"


def test_sensor_unique_id_is_stable_for_same_site() -> None:
    assert sensor_unique_id("site-1", "electricity.consumption") == (
        "site-1_electricity.consumption"
    )


def test_sensor_unique_id_prefers_glow_resource_id() -> None:
    first = sensor_unique_id(
        "site-1",
        "electricity.consumption",
        "resource-123",
    )
    second = sensor_unique_id(
        "site-2",
        "electricity.consumption",
        "resource-123",
    )

    assert first == "resource-123_electricity.consumption"
    assert second == first
