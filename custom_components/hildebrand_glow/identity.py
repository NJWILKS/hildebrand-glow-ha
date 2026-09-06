"""Stable identity helpers for Hildebrand Glow entities and config entries."""
from __future__ import annotations


def site_identity(virtual_entity_id: str | None, entry_id: str) -> str:
    """Return a stable site identity, falling back for legacy entries."""
    return virtual_entity_id or entry_id


def config_unique_id(username: str, virtual_entity_id: str) -> str:
    """Return a stable config-entry identity for one Bright account site."""
    return f"{username.strip().lower()}:{virtual_entity_id}"


def sensor_unique_id(site_id: str, sensor_key: str) -> str:
    """Return a stable entity unique ID for a meter site and sensor."""
    return f"{site_id}_{sensor_key}"
