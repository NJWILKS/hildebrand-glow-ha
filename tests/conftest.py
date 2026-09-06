from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the repository root is importable when pytest is launched by GitHub Actions.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
