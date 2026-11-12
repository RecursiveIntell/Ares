"""Host-native marker support for the standalone workflow tests."""
import sys
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "linux_only: runs on a real Linux host")


def pytest_collection_modifyitems(items):
    if sys.platform != "linux":
        for item in items:
            if item.get_closest_marker("linux_only"):
                item.add_marker(pytest.mark.skip(reason="requires the native Linux containment backend"))
