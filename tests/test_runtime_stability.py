import time


def test_global_rotation_resets_timer_when_no_devices_are_assigned(monkeypatch):
    import app.main as main

    class Registry:
        def get_all_devices(self):
            return []

    class Config:
        proxies = [object(), object()]

    before = time.time()
    main._rotate_state["last_rotate_time"] = 0

    count = main.perform_auto_rotation(Config(), Registry(), None)

    assert count == 0
    assert main._rotate_state["last_rotate_time"] >= before


def test_hotspot_failure_does_not_continue_with_wired_lan(monkeypatch):
    from app.network_setup import resolve_hotspot_topology

    result = resolve_hotspot_topology(
        configured_lan_interface="Ethernet 3",
        hotspot_adapter=None,
        hotspot_active=False,
    )

    assert result["ready"] is False
    assert result["lan_interface"] is None
    assert "not active" in result["reason"].lower()


def test_hotspot_topology_uses_active_ics_adapter():
    from app.network_setup import resolve_hotspot_topology

    result = resolve_hotspot_topology(
        configured_lan_interface="Ethernet 3",
        hotspot_adapter="Local Area Connection* 10",
        hotspot_active=True,
    )

    assert result == {
        "ready": True,
        "lan_interface": "Local Area Connection* 10",
        "reason": "",
    }


def test_hotspot_topology_requires_active_adapter_even_when_wired_lan_exists():
    from app.network_setup import resolve_hotspot_topology

    for _ in range(100):
        result = resolve_hotspot_topology("Ethernet 3", None, False)
        assert result["ready"] is False
        assert result["lan_interface"] is None


def test_rotation_storm_guard_resets_timer_repeatedly_without_devices():
    import app.main as main

    class Registry:
        def get_all_devices(self):
            return []

    class Config:
        proxies = [object(), object()]

    for _ in range(100):
        main._rotate_state["last_rotate_time"] = 0
        assert main.perform_auto_rotation(Config(), Registry(), None) == 0
        assert main._rotate_state["last_rotate_time"] > 0
