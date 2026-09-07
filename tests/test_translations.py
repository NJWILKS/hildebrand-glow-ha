from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "hildebrand_glow"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_english_options_translation_matches_strings() -> None:
    """Keep the user-facing options flow copy in sync with strings.json."""
    strings = _load_json(INTEGRATION / "strings.json")
    english = _load_json(INTEGRATION / "translations" / "en.json")

    assert english["options"] == strings["options"]

    menu = english["options"]["step"]["init"]["menu_options"]
    assert menu["settings"].strip()
    assert menu["reset_history"].strip()
