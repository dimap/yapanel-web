import os
import json
import tempfile
import pytest
from pathlib import Path
import party


def test_party_bulb_crud():
    with tempfile.TemporaryDirectory() as tmpdir:
        bulbs_path = os.path.join(tmpdir, "bulbs.json")
        settings_path = os.path.join(tmpdir, "settings.json")
        
        pm = party.PartyManager(bulbs_config_path=bulbs_path, settings_path=settings_path)
        
        # 1. Initial bulbs should be empty
        assert pm.get_bulbs() == []
        
        # 2. Add bulb
        res = pm.add_bulb({
            "id": "bf11223344556677889900",
            "name": "Люстра центр",
            "ip": "192.168.1.50",
            "key": "abcdef0123456789",
            "role": "kick",
            "side": "",
            "ver": 3.5,
            "enabled": True
        })
        assert res.get("status") == "ok"
        assert res["bulb"]["id"] == "bf11223344556677889900"
        
        # 3. Check get_bulbs
        bulbs = pm.get_bulbs()
        assert len(bulbs) == 1
        assert bulbs[0]["name"] == "Люстра центр"
        assert bulbs[0]["ip"] == "192.168.1.50"
        assert bulbs[0]["role"] == "kick"
        
        # 4. Update bulb
        res2 = pm.add_bulb({
            "id": "bf11223344556677889900",
            "name": "Люстра (Обновлено)",
            "ip": "192.168.1.51",
            "key": "abcdef0123456789",
            "role": "ambient",
            "side": "left"
        })
        assert res2.get("status") == "ok"
        bulbs = pm.get_bulbs()
        assert len(bulbs) == 1
        assert bulbs[0]["name"] == "Люстра (Обновлено)"
        assert bulbs[0]["ip"] == "192.168.1.51"
        assert bulbs[0]["role"] == "ambient"
        assert bulbs[0]["pos_x"] == -1.0
        
        # 5. Delete bulb
        del_res = pm.delete_bulb("bf11223344556677889900")
        assert del_res.get("status") == "ok"
        assert pm.get_bulbs() == []


def test_party_bulb_import():
    with tempfile.TemporaryDirectory() as tmpdir:
        bulbs_path = os.path.join(tmpdir, "bulbs.json")
        settings_path = os.path.join(tmpdir, "settings.json")
        pm = party.PartyManager(bulbs_config_path=bulbs_path, settings_path=settings_path)
        
        devices_data = [
            {"id": "dev1", "name": "Бра левая", "ip": "192.168.1.60", "key": "key1", "role": "snare", "side": "left"},
            {"gwId": "dev2", "name": "Бра правая", "ip": "192.168.1.61", "local_key": "key2", "role": "snare", "side": "right"}
        ]
        
        res = pm.import_bulbs(devices_data)
        assert res.get("status") == "ok"
        assert res.get("count") == 2
        
        bulbs = pm.get_bulbs()
        assert len(bulbs) == 2
        ids = {b["id"] for b in bulbs}
        assert "dev1" in ids and "dev2" in ids


def test_tuya_udp_packet_parsing():
    sample_json = b'some_bytes{"ip":"192.168.1.80","gwId":"dev_tuya_abc","version":"3.3"}trailer'
    parsed = party.PartyManager._parse_tuya_udp_packet(sample_json)
    assert parsed is not None
    assert parsed.get("ip") == "192.168.1.80"
    assert parsed.get("gwId") == "dev_tuya_abc"
    assert parsed.get("version") == "3.3"


def test_panel_party_bulb_methods():
    import server
    p = server.Panel.__new__(server.Panel)
    p.config = None
    p._party_lock = None
    p.party = None
    
    with tempfile.TemporaryDirectory() as tmpdir:
        bulbs_path = os.path.join(tmpdir, "bulbs.json")
        p.party = party.PartyManager(bulbs_config_path=bulbs_path)
        
        # Test get empty
        assert p.party_bulbs_get() == []
        
        # Test add
        add_res = p.party_bulb_add({
            "id": "test_id_99",
            "name": "Тестовая лампа",
            "ip": "192.168.1.99",
            "key": "0123456789abcdef",
            "role": "kick"
        })
        assert add_res["status"] == "ok"
        
        # Test get
        bulbs = p.party_bulbs_get()
        assert len(bulbs) == 1
        assert bulbs[0]["id"] == "test_id_99"
        
        # Test delete
        del_res = p.party_bulb_delete("test_id_99")
        assert del_res["status"] == "ok"
        assert p.party_bulbs_get() == []
