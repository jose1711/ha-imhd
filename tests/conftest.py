import pytest
pytest_plugins = "pytest_homeassistant_custom_component"
@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


from unittest.mock import patch as _patch
from custom_components.imhd_sk.stops import Stop

FAKE_STOPS = [
    Stop(id=93, name="Hodžovo námestie", lat=48.1446, lng=17.1077, platform_labels={"1": "A", "2": "B"}),
    Stop(id=341, name="Na križovatkách", lat=48.168, lng=17.18, platform_labels={"837": "A", "838": "B"}),
]

@pytest.fixture(autouse=True)
def fake_stop_list():
    async def _get(session, timeout=10):
        return FAKE_STOPS
    with _patch("custom_components.imhd_sk.config_flow.get_stops", _get), \
         _patch("custom_components.imhd_sk.get_stops", _get):
        yield
