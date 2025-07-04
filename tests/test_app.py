import pytest
from unittest.mock import patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import app
app = app.app


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


@patch("app.subprocess.run")
def test_index_get(mock_run, client):
    # Simulate interfaces returned by get_interfaces
    mock_run.return_value.stdout = "eth0\nlo\n"
    mock_run.return_value.stderr = ""
    response = client.get("/")
    assert response.status_code == 200
    assert b"eth0" in response.data or b"lo" in response.data


@patch("app.subprocess.run")
def test_apply_shaping(mock_run, client):
    mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)

    form_data = {
        "apply": "1",
        "interface": "eth0",
        "rate": "10mbit",
        "loss": "2",
        "duplicate": "1",
        "protocol": "tcp"
    }
    response = client.post("/", data=form_data, follow_redirects=True)
    assert response.status_code == 200


@patch("app.subprocess.run")
def test_interface_selection(mock_run, client):
    mock_run.return_value.stdout = "eth0\n"
    mock_run.return_value.stderr = ""
    form_data = {"interface": "eth0"}
    response = client.post("/", data=form_data)
    assert response.status_code == 200


@patch("app.subprocess.run")
def test_clear(mock_run, client):
    mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
    response = client.post("/clear", data={"interface": "eth0"}, follow_redirects=True)
    assert response.status_code == 200


@patch("app.subprocess.run")
def test_reset(mock_run, client):
    mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
    response = client.post("/reset", data={"interface": "eth0"})
    assert response.status_code == 200
    assert b"Traffic shaping reset on eth0" in response.data


@patch("app.subprocess.run")
def test_status(mock_run, client):
    mock_run.return_value.stdout = "qdisc htb 1: root ..."
    mock_run.return_value.stderr = ""
    response = client.post("/status", data={"interface": "eth0"})
    assert response.status_code == 200


@pytest.mark.skip(reason="need to generate correct error message")
@patch("app.subprocess.run")
def test_invalid_interface_handling(mock_run, client):
    def side_effect(cmd, *args, **kwargs):
        if "nonexistent0" in cmd:
            if "|| true" in cmd:
                return MagicMock(stdout="", stderr="", returncode=0)
            else:
                return MagicMock(stdout="", stderr="Cannot find device nonexistent0", returncode=1)
        return MagicMock(stdout="", stderr="", returncode=0)

    mock_run.side_effect = side_effect

    form_data = {
        "apply": "1",
        "interface": "nonexistent0",
        "rate": "1mbit",
        "loss": "0",
        "duplicate": "0",
        "protocol": "all"
    }
    response = client.post("/", data=form_data)
    assert response.status_code == 200

    # Check if error message is present in the response (adjust message accordingly)
    assert (b"Invalid interface" in response.data or
            b"Cannot find device" in response.data or
            b"Error applying settings to interface" in response.data)

