from homeassistant.helpers.translation import async_get_translations
async def test_translations_complete(hass):
    for cat in ("config", "options", "selector", "services"):
        sk = await async_get_translations(hass, "sk", cat, ["imhd_sk"])
        en = await async_get_translations(hass, "en", cat, ["imhd_sk"])
        assert set(sk) == set(en), (cat, set(en) ^ set(sk))
        assert sk, cat
