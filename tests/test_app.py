import pytest
from app import app

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client

def test_interface_config_isolation(client, monkeypatch):
    # Fake interfaces list
    monkeypatch.setattr("app.get_interfaces", lambda: ["lo", "eth0"])

    # Fake tc config to simulate 'lo' configured, 'eth0' unconfigured
    def fake_parse_tc_config(iface):
        if iface == "lo":
            return {
                "rate": "1mbit",
                "loss": "0.5",
                "duplicate": "1.0",
                "protocol": "tcp",
                "interface": "lo"
            }
        else:
            return {
                "rate": "",
                "loss": "",
                "duplicate": "",
                "protocol": "",
                "interface": "eth0"
            }

    monkeypatch.setattr("app.parse_tc_config", fake_parse_tc_config)

    # Access lo interface (configured)
    res_lo = client.get("/?iface=lo")
    assert b'value="1mbit"' in res_lo.data
    assert b'value="0.5"' in res_lo.data
    assert b'value="1.0"' in res_lo.data
    assert b'<option value="tcp" selected>' in res_lo.data

    # Access eth0 interface (unconfigured)
    res_eth0 = client.get("/?iface=eth0")
    assert b'value="1mbit"' not in res_eth0.data
    assert b'value="0.5"' not in res_eth0.data
    assert b'value="1.0"' not in res_eth0.data
    assert b'<option value="tcp" selected>' not in res_eth0.data
    # Empty values should be present
    assert b'value=""' in res_eth0.data
    assert b'<option value="" selected>' in res_eth0.data
