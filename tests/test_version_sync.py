"""Tests that the integration version stays consistent across files."""

import json
from pathlib import Path

from custom_components.climate_advisor.const import VERSION


MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "climate_advisor"
    / "manifest.json"
)


class TestVersionSync:
    """VERSION in const.py must match manifest.json."""

    def test_version_matches_manifest(self):
        manifest = json.loads(MANIFEST_PATH.read_text())
        assert manifest["version"] == VERSION, (
            f"const.py VERSION ({VERSION}) != manifest.json version "
            f"({manifest['version']}). Update both when releasing."
        )

    def test_version_is_semver(self):
        """VERSION must be a valid semver string (MAJOR.MINOR.PATCH)."""
        parts = VERSION.split(".")
        assert len(parts) == 3, f"VERSION '{VERSION}' is not MAJOR.MINOR.PATCH"
        for part in parts:
            assert part.isdigit(), f"VERSION segment '{part}' is not numeric"
