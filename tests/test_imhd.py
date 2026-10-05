import asyncio
import time
from datetime import timedelta
from unittest.mock import patch
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from custom_components.imhd_sk.api import parse_tabs, ImhdError
from custom_components.imhd_sk.const import DOMAIN

def board(offset_min=0):
    now = int(time.time()*1000) + offset_min*60000
    return [{"nastupiste":1,"tab":[
        {"linka":"9","issi":"7416","cielStr":"Karlova Ves","cas":now+300000,"casDelta":2,"typ":"online","predoslaZstr":"Bratislava, Kollárovo nám."},
        {"linka":"4","konecnaZstr":"Dúbravka","cas":now+120000,"typ":"cp"}]},
        {"nastupiste":2,"tab":[{"linka":"39","cas":now+600000,"cielStr":"Patrónka"}]}]

async def fake_fetch(stop_id, *, timeout=10, settle=2.0, http_session=None):
    if stop_id == 999: raise ImhdError("timeout")
    return parse_tabs(board()), ["oznam"]

P = "custom_components.imhd_sk.api.fetch_board"

async def test_action_entry_and_service(hass):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] == FlowResultType.MENU
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "action"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    with patch("custom_components.imhd_sk.api.fetch_board", fake_fetch):
        resp = await hass.services.async_call(DOMAIN, "get_departures", {"stop_id": 93, "lines": "9, 39", "limit": 5}, blocking=True, return_response=True)
    assert [d["line"] for d in resp["departures"]] == ["9", "39"]
    assert resp["departures"][0]["delay"] == 2 and resp["departures"][0]["realtime"]
    assert resp["info"] == ["oznam"]
    assert resp["platforms"] == {"1": ["Dúbravka", "Karlova Ves"], "2": ["Patrónka"]}
    with patch("custom_components.imhd_sk.api.fetch_board", fake_fetch):
        resp = await hass.services.async_call(DOMAIN, "get_departures", {"stop_id": 93, "platforms": "2"}, blocking=True, return_response=True)
    assert [d["line"] for d in resp["departures"]] == ["39"]
    with patch("custom_components.imhd_sk.api.fetch_board", fake_fetch):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(DOMAIN, "get_departures", {"stop_id": 999}, blocking=True, return_response=True)

async def test_poll_stop(hass):
    from homeassistant.setup import async_setup_component
    assert await async_setup_component(hass, "homeassistant", {})
    with patch("custom_components.imhd_sk.config_flow.fetch_board", fake_fetch), patch("custom_components.imhd_sk.source.fetch_board", fake_fetch):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "stop"})
        assert r["step_id"] == "stop"
        bad = await hass.config_entries.flow.async_configure(r["flow_id"], {"stop": "999", "mode": "poll", "lines": "", "max_departures": 10})
        assert bad["errors"] == {"base": "cannot_connect"}
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"stop": "93", "name": "Domov", "mode": "poll", "lines": "9", "max_departures": 5})
        assert r["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        states = {s.entity_id: s for s in hass.states.async_all("sensor")}
        print(sorted(states))
        nxt = states["sensor.domov_najblizsi_odchod"]
        assert nxt.attributes["line"] == "9" and nxt.attributes["minutes"] in (4, 5)
        assert "sensor.domov_linka_9_a" in states  # filter "9" -> only line 9 sensors
        assert not any("linka_4" in e or "linka_39" in e for e in states)
        # options flow incl. scan interval
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        o = await hass.config_entries.options.async_init(entry.entry_id)
        assert "scan_interval" in str(o["data_schema"].schema)
        o = await hass.config_entries.options.async_configure(o["flow_id"], {"lines": "", "max_departures": 5, "scan_interval": 1})
        await hass.async_block_till_done()
        s = hass.states.get("sensor.domov_najblizsi_odchod")
        assert s.attributes["line"] == "4"
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
        await hass.async_block_till_done()
        await hass.services.async_call("homeassistant", "update_entity", {"entity_id": "sensor.domov_najblizsi_odchod"}, blocking=True)
        assert hass.config_entries.async_entries(DOMAIN)[0].state.value == "loaded"
        assert await hass.config_entries.async_unload(entry.entry_id)

class FakeSio:
    script = None  # list of (delay_s, payload) sent after connect, in background
    fail_next = 0  # number of upcoming connects that fail
    instances: list = []
    def __init__(self, **kw):
        self.h = {}; self.emitted = []; self.kw = kw; self.closed = False
        FakeSio.instances.append(self)
    def event(self, f): self.h[f.__name__] = f; return f
    def on(self, name):
        def d(f): self.h[name] = f; return f
        return d
    async def connect(self, *a, **k):
        if FakeSio.fail_next:
            FakeSio.fail_next -= 1
            import socketio
            raise socketio.exceptions.ConnectionError("refused")
        await self.h["connect"]()
        if FakeSio.script is None:
            await self.h["tabs"](board())
            return
        async def run():
            for delay, payload in FakeSio.script:
                await asyncio.sleep(delay)
                await self.h["tabs"](payload)
        self._task = asyncio.get_running_loop().create_task(run())
    async def emit(self, *a): self.emitted.append(a)
    async def disconnect(self):
        if not self.closed:
            self.closed = True
            await self.h["disconnect"]()
    async def server_kick(self):
        """Server-initiated disconnect (python-socketio would NOT reconnect)."""
        self.closed = True
        await self.h["disconnect"]("io server disconnect")

async def test_push_stop(hass):
    holder = {}
    def mk(**kw):
        holder["sio"] = FakeSio(**kw); return holder["sio"]
    with patch("custom_components.imhd_sk.config_flow.fetch_board", fake_fetch), patch("custom_components.imhd_sk.source.socketio.AsyncClient", mk):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "stop"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"stop": "93", "mode": "push", "lines": "", "max_departures": 10})
        await hass.async_block_till_done()
        sio = holder["sio"]
        assert sio.emitted[0] == ("tabStart", [93, "*"])
        s = hass.states.get("sensor.hodzovo_namestie_najblizsi_odchod")
        assert s.attributes["line"] == "4" and s.attributes["connected"]
        p1 = hass.states.get("sensor.hodzovo_namestie_nastupiste_a")
        p2 = hass.states.get("sensor.hodzovo_namestie_nastupiste_b")
        assert p1.attributes["line"] == "4" and p1.attributes["destinations"] == ["Dúbravka", "Karlova Ves"]
        assert p2.attributes["line"] == "39" and p2.attributes["platform"] == "2"
        assert [d["line"] for d in p2.attributes["departures"]] == ["39"]
        assert hass.states.get("sensor.hodzovo_namestie_nastupiste_3") is None
        new_plat = {"nastupiste": 3, "tab": [{"linka": "N33", "cas": int(time.time()*1000) + 900000, "cielStr": "Noc"}]}
        await sio.h["tabs"]([new_plat])  # new platform appears -> new sensor
        await hass.async_block_till_done()
        p3 = hass.states.get("sensor.hodzovo_namestie_nastupiste_3")
        assert p3 is not None and p3.attributes["line"] == "N33"
        await sio.h["tabs"]([board()[1]])  # only platform 2 -> platform 1 must stay
        await hass.async_block_till_done()
        s = hass.states.get("sensor.hodzovo_namestie_najblizsi_odchod")
        assert [d["line"] for d in s.attributes["departures"]] == ["4", "9", "39", "N33"]
        await sio.h["tabs"](board(offset_min=-3))  # push: everything 3 min earlier -> line 4 gone
        await hass.async_block_till_done()
        s = hass.states.get("sensor.hodzovo_namestie_najblizsi_odchod")
        assert s.attributes["line"] == "9", s.attributes
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert await hass.config_entries.async_unload(entry.entry_id)


def split_board():
    b = board()
    return [b[0]], [b[1]]

async def test_one_shot_collects_platforms_until_settled():
    from custom_components.imhd_sk import api
    p1, p2 = split_board()
    FakeSio.script = [(0, p1), (0.3, p2)]
    try:
        with patch("custom_components.imhd_sk.api.socketio.AsyncClient", FakeSio):
            deps, _ = await api.fetch_board(93, timeout=5, settle=0.6)
        assert sorted(d.line for d in deps) == ["39", "4", "9"]
        # platform 2 arrives after the settle window -> not waited for
        FakeSio.script = [(0, p1), (1.0, p2)]
        with patch("custom_components.imhd_sk.api.socketio.AsyncClient", FakeSio):
            deps, _ = await api.fetch_board(93, timeout=5, settle=0.3)
        assert sorted(d.line for d in deps) == ["4", "9"]
    finally:
        FakeSio.script = None

async def test_one_shot_timeout_without_data():
    from custom_components.imhd_sk import api
    FakeSio.script = []
    try:
        with patch("custom_components.imhd_sk.api.socketio.AsyncClient", FakeSio):
            with pytest.raises(ImhdError):
                await api.fetch_board(93, timeout=0.5, settle=0.2)
    finally:
        FakeSio.script = None

def test_board_merges_by_platform():
    from custom_components.imhd_sk.api import Board
    p1, p2 = split_board()
    b = Board()
    b.update(p1); b.update(p2)
    assert sorted(d.line for d in b.departures) == ["39", "4", "9"]
    b.update([{"nastupiste": 2, "tab": []}])  # platform 2 emptied, platform 1 kept
    assert sorted(d.line for d in b.departures) == ["4", "9"]


async def test_platform_sensors_can_be_disabled(hass):
    with patch("custom_components.imhd_sk.config_flow.fetch_board", fake_fetch), patch("custom_components.imhd_sk.source.fetch_board", fake_fetch):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "stop"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"stop": "93", "mode": "poll", "lines": "", "max_departures": 5, "platform_sensors": False, "line_sensors": False})
        await hass.async_block_till_done()
        ids = sorted(s.entity_id for s in hass.states.async_all("sensor"))
        assert ids == ["sensor.hodzovo_namestie_najblizsi_odchod"], ids


async def test_resolve_stop_id(hass, aioclient_mock):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from custom_components.imhd_sk.stops import resolve_stop_id
    page = "https://imhd.sk/ba/zastavka/Na-kri%C5%BEovatk%C3%A1ch/ca71b671897182838071cc"
    aioclient_mock.get(page, text='<a href="/ba/online-zastavkova-tabula?st=341&amp;ssd=1">tabuľa</a>')
    session = async_get_clientsession(hass)
    assert await resolve_stop_id(session, "341") == 341
    assert await resolve_stop_id(session, " https://imhd.sk/ba/online-zastavkova-tabula?st=341&ssd=1 ") == 341
    assert await resolve_stop_id(session, page) == 341
    assert await resolve_stop_id(session, "Na križovatkách") is None

async def test_stop_flow_by_url_uses_stop_name_and_labels(hass, aioclient_mock):
    page = "https://imhd.sk/ba/zastavka/Na-kri%C5%BEovatk%C3%A1ch/ca71b671897182838071cc"
    aioclient_mock.get(page, text='...?st=341&amp;ssd=1...')
    async def fetch341(stop_id, *, timeout=10, settle=2.0, http_session=None):
        assert stop_id == 341
        now = int(time.time()*1000)
        return parse_tabs([{"nastupiste": 837, "tab": [{"linka": "96", "cielStr": "Prokofievova", "cas": now+120000}]},
                           {"nastupiste": 838, "tab": [{"linka": "61", "cielStr": "Letisko", "cas": now+240000}]}]), []
    with patch("custom_components.imhd_sk.config_flow.fetch_board", fetch341), patch("custom_components.imhd_sk.source.fetch_board", fetch341):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "stop"})
        # dropdown offers stops, nearest first is computed from hass home
        sel = r["data_schema"].schema["stop"]
        assert {o["value"] for o in sel.config["options"]} == {"93", "341"}
        bad = await hass.config_entries.flow.async_configure(r["flow_id"], {"stop": "xyz", "mode": "poll", "lines": "", "max_departures": 5, "platform_sensors": True})
        assert bad["errors"] == {"stop": "invalid_stop"}
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"stop": page, "mode": "poll", "lines": "", "max_departures": 5, "platform_sensors": True})
        assert r["type"] == FlowResultType.CREATE_ENTRY and r["title"] == "Na križovatkách"
        assert r["data"]["stop_id"] == 341 and r["data"]["platform_labels"] == {"837": "A", "838": "B"}
        await hass.async_block_till_done()
        a = hass.states.get("sensor.na_krizovatkach_nastupiste_a")
        b = hass.states.get("sensor.na_krizovatkach_nastupiste_b")
        assert a.attributes["platform"] == "837" and a.attributes["platform_label"] == "A" and a.attributes["line"] == "96"
        assert b.attributes["destinations"] == ["Letisko"]
        nxt = hass.states.get("sensor.na_krizovatkach_najblizsi_odchod")
        assert nxt.attributes["departures"][0]["platform_label"] == "A"

async def test_old_entry_gets_labels_backfilled(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, unique_id="341", title="Na križovatkách",
        data={"entry_type": "stop", "stop_id": 341, "name": "Na križovatkách", "mode": "poll"},
        options={"lines": "", "max_departures": 5, "scan_interval": 2})
    entry.add_to_hass(hass)
    with patch("custom_components.imhd_sk.source.fetch_board", fake_fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.data["platform_labels"] == {"837": "A", "838": "B"}

async def test_action_accepts_url(hass, aioclient_mock):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "action"})
    await hass.config_entries.flow.async_configure(r["flow_id"], {})
    await hass.async_block_till_done()
    calls = []
    async def fetch(stop_id, **kw):
        calls.append(stop_id); return parse_tabs(board()), []
    with patch("custom_components.imhd_sk.api.fetch_board", fetch):
        resp = await hass.services.async_call(DOMAIN, "get_departures", {"stop_id": "https://imhd.sk/ba/online-zastavkova-tabula?st=341&ssd=1"}, blocking=True, return_response=True)
    assert calls == [341] and resp["stop_name"] == "Na križovatkách" and resp["platform_labels"]["837"] == "A"
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "get_departures", {"stop_id": "nič"}, blocking=True, return_response=True)


async def test_line_platform_sensors(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from homeassistant.helpers import entity_registry as er
    entry = MockConfigEntry(domain=DOMAIN, unique_id="93", title="Hodžovo námestie",
        data={"entry_type": "stop", "stop_id": 93, "name": "Hodžovo námestie", "mode": "push",
              "platform_labels": {"1": "A", "2": "B"}},
        options={"lines": "", "max_departures": 5})
    entry.add_to_hass(hass)
    # leftover entity from <=0.5 (per-line without direction) must be cleaned up
    reg = er.async_get(hass)
    reg.async_get_or_create("sensor", DOMAIN, "93_line_9", config_entry=entry, suggested_object_id="hodzovo_namestie_linka_9")
    holder = {}
    def mk(**kw):
        holder["sio"] = FakeSio(**kw); return holder["sio"]
    now = int(time.time()*1000)
    with patch("custom_components.imhd_sk.source.socketio.AsyncClient", mk):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert reg.async_get("sensor.hodzovo_namestie_linka_9") is None
        s9a = hass.states.get("sensor.hodzovo_namestie_linka_9_a")
        assert s9a.name == "Hodžovo námestie Linka 9 · A"
        assert s9a.attributes["line"] == "9" and s9a.attributes["platform_label"] == "A"
        assert s9a.attributes["destination"] == "Karlova Ves" and s9a.attributes["delay"] == 2
        assert s9a.attributes["destinations"] == ["Karlova Ves"]
        assert [d["line"] for d in s9a.attributes["departures"]] == ["9"]
        assert hass.states.get("sensor.hodzovo_namestie_linka_39_b").attributes["destination"] == "Patrónka"
        # same line in the other direction appears -> new sensor
        await holder["sio"].h["tabs"]([{"nastupiste": 2, "tab": [
            {"linka": "39", "cas": now + 600000, "cielStr": "Patrónka"},
            {"linka": "9", "cas": now + 420000, "cielStr": "Ružinov"}]}])
        await hass.async_block_till_done()
        s9b = hass.states.get("sensor.hodzovo_namestie_linka_9_b")
        assert s9b is not None and s9b.attributes["destination"] == "Ružinov"
        # line 9 towards A still only has Karlova Ves
        assert hass.states.get("sensor.hodzovo_namestie_linka_9_a").attributes["destination"] == "Karlova Ves"
        assert await hass.config_entries.async_unload(entry.entry_id)

    # after restart the registered line sensors exist immediately, even before data
    FakeSio.script = []
    try:
        with patch("custom_components.imhd_sk.source.socketio.AsyncClient", FakeSio), \
             patch("custom_components.imhd_sk.source.PushSource.start", new=lambda self: asyncio.sleep(0)):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            assert hass.states.get("sensor.hodzovo_namestie_linka_9_b") is not None
    finally:
        FakeSio.script = None



@pytest.fixture
def fresh_fake():
    FakeSio.instances = []; FakeSio.fail_next = 0; FakeSio.script = None
    yield
    FakeSio.instances = []; FakeSio.fail_next = 0; FakeSio.script = None


def make_push(hass, **kw):
    from custom_components.imhd_sk.source import PushSource
    params = dict(timeout=1, watchdog=0.5, check_interval=0.05, backoff_min=0.05, backoff_max=0.2)
    params.update(kw)
    return PushSource(hass, 93, None, **params)


async def wait_until(cond, timeout=3):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.02)


async def test_push_reconnects_after_server_kick(hass, fresh_fake):
    with patch("custom_components.imhd_sk.source.socketio.AsyncClient", FakeSio):
        src = make_push(hass, watchdog=60)
        await src.start()
        assert src.connected and src.healthy
        assert FakeSio.instances[0].kw["reconnection"] is False  # we supervise ourselves
        await FakeSio.instances[0].server_kick()
        await wait_until(lambda: src.reconnects == 1)
        assert len(FakeSio.instances) == 2
        assert FakeSio.instances[1].emitted[0] == ("tabStart", [93, "*"])
        assert src.connected and src.last_error is None
        await src.stop()
        assert FakeSio.instances[1].closed and not src.connected


async def test_push_watchdog_reconnects_silent_connection(hass, fresh_fake):
    FakeSio.script = [(0, board())]  # first board only, then silence
    with patch("custom_components.imhd_sk.source.socketio.AsyncClient", FakeSio):
        src = make_push(hass, watchdog=0.3)
        await src.start()
        await wait_until(lambda: src.reconnects >= 1)
        assert FakeSio.instances[0].closed  # silent connection was dropped
        await src.stop()


async def test_push_backoff_until_server_back(hass, fresh_fake):
    with patch("custom_components.imhd_sk.source.socketio.AsyncClient", FakeSio):
        src = make_push(hass, watchdog=60)
        await src.start()
        FakeSio.fail_next = 3
        await FakeSio.instances[0].server_kick()
        await wait_until(lambda: not src.connected)
        await wait_until(lambda: src.reconnects == 1)
        assert len(FakeSio.instances) == 5  # 1 + 3 failed + 1 ok
        assert src.connected and src.last_error is None
        await src.stop()


async def test_push_first_connect_failure_raises(hass, fresh_fake):
    FakeSio.fail_next = 1
    with patch("custom_components.imhd_sk.source.socketio.AsyncClient", FakeSio):
        src = make_push(hass)
        with pytest.raises(ImhdError):
            await src.start()
        assert src.last_error == "refused"


async def test_connectivity_binary_sensor(hass, fresh_fake):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, unique_id="93", title="Hodžovo námestie",
        data={"entry_type": "stop", "stop_id": 93, "name": "Hodžovo námestie", "mode": "push",
              "platform_labels": {"1": "A", "2": "B"}},
        options={"lines": "", "max_departures": 5})
    entry.add_to_hass(hass)
    with patch("custom_components.imhd_sk.source.socketio.AsyncClient", FakeSio), \
         patch("custom_components.imhd_sk.WATCHDOG_SECONDS", 60, create=True):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        st = hass.states.get("binary_sensor.hodzovo_namestie_pripojenie")
        assert st.state == "on" and st.attributes["mode"] == "push" and st.attributes["reconnects"] == 0
        FakeSio.fail_next = 1000  # server stays down
        await FakeSio.instances[0].server_kick()
        await hass.async_block_till_done()
        st = hass.states.get("binary_sensor.hodzovo_namestie_pripojenie")
        assert st.state == "off" and st.attributes["connected"] is False
        assert await hass.config_entries.async_unload(entry.entry_id)



async def test_manual_mode_never_fetches_on_its_own(hass, fresh_fake):
    from homeassistant.setup import async_setup_component
    assert await async_setup_component(hass, "homeassistant", {})
    calls = []
    async def counting_fetch(stop_id, **kw):
        calls.append(stop_id)
        return parse_tabs(board()), []
    with patch("custom_components.imhd_sk.config_flow.fetch_board", counting_fetch), \
         patch("custom_components.imhd_sk.source.fetch_board", counting_fetch), \
         patch("custom_components.imhd_sk.source.socketio.AsyncClient", FakeSio):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "stop"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"stop": "93", "mode": "manual", "lines": "", "max_departures": 5})
        assert r["type"] == FlowResultType.CREATE_ENTRY
        assert "scan_interval" not in r["options"]
        await hass.async_block_till_done()
        assert calls == [93]  # only the validation in the config flow
        calls.clear()
        assert FakeSio.instances == []  # no persistent connection either

        nxt = hass.states.get("sensor.hodzovo_namestie_najblizsi_odchod")
        assert nxt.state == "unknown"  # available, but nothing fetched yet
        conn = hass.states.get("binary_sensor.hodzovo_namestie_pripojenie")
        assert conn.state == "unknown" and conn.attributes["mode"] == "manual"

        # time passes -> still nothing fetched
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=2))
        await hass.async_block_till_done()
        assert calls == []

        # button press -> one fetch
        await hass.services.async_call("button", "press", {"entity_id": "button.hodzovo_namestie_obnovit"}, blocking=True)
        await hass.async_block_till_done()
        assert calls == [93]
        nxt = hass.states.get("sensor.hodzovo_namestie_najblizsi_odchod")
        assert nxt.attributes["line"] == "4"
        assert hass.states.get("sensor.hodzovo_namestie_linka_9_a") is not None
        assert hass.states.get("binary_sensor.hodzovo_namestie_pripojenie").state == "on"

        # update_entity -> one more fetch
        await hass.services.async_call("homeassistant", "update_entity", {"entity_id": "sensor.hodzovo_namestie_najblizsi_odchod"}, blocking=True)
        assert calls == [93, 93]

        # and again, hours later, still no automatic fetch; data stays available
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=5))
        await hass.async_block_till_done()
        assert calls == [93, 93]
        assert hass.states.get("sensor.hodzovo_namestie_najblizsi_odchod").state != "unavailable"

        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_refresh_button_only_for_poll_and_manual(hass, fresh_fake):
    with patch("custom_components.imhd_sk.config_flow.fetch_board", fake_fetch), \
         patch("custom_components.imhd_sk.source.fetch_board", fake_fetch), \
         patch("custom_components.imhd_sk.source.socketio.AsyncClient", FakeSio):
        for stop, mode in (("93", "push"), ("341", "poll")):
            r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
            r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "stop"})
            await hass.config_entries.flow.async_configure(r["flow_id"], {"stop": stop, "mode": mode, "lines": "", "max_departures": 5})
            await hass.async_block_till_done()
        buttons = sorted(s.entity_id for s in hass.states.async_all("button"))
        assert buttons == ["button.na_krizovatkach_obnovit"], buttons
        for e in hass.config_entries.async_entries(DOMAIN):
            assert await hass.config_entries.async_unload(e.entry_id)
