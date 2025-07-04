import subprocess
import time
import pytest
import requests
import psutil
import re

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import app
app = app.app

# from unittest.mock import patch, MagicMock
#
# @pytest.fixture
# def client():
#     app.config["TESTING"] = True
#     with app.test_client() as client:
#         yield client
#
#
# @pytest.fixture(autouse=True)
# def patch_subprocess_run():
#     with patch("app.subprocess.run") as mock_run:
#         mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
#         yield mock_run


# run from root dir:  pytest -s tests/test_integration.py
# test may leave interfaces in a bad state, if so:  ./resetifs.sh

def get_ip_address(interface_name):
    addrs = psutil.net_if_addrs()
    iface = addrs.get(interface_name)
    if not iface:
        return None
    for addr in iface:
        if addr.family.name == 'AF_INET':
            return addr.address
    return None



def send_apply(iface="enp0s25", rate="1mbit", loss=0, duplicate=0, protocol="tcp", delay=0, url="http://127.0.0.1:5000/"):
    import socket
    s = socket.create_connection(("127.0.0.1", 5000), timeout=2)
    print("socket connect succeeded")
    s.close()

    r = requests.post(url, data = {
                            "apply": 1,
                            "interface": iface,
                            "rate": rate,
                            "loss": loss,
                            "duplicate": duplicate,
                            "delay": delay,
                            "protocol": protocol}
    )
    assert r.status_code == 200
    # print("APPLY OUTPUT:\n", r.text)
    return r.status_code == 200, r.text


def send_reset(iface="enp0s25", url="http://localhost:5000/reset"):
    r = requests.post(url, data = {"interface": iface})
    assert r.status_code == 200
    # print("RESET OUTPUT:\n", r.text)
    return r.status_code == 200, r.text


def get_status(iface="enp0s25", url="http://127.0.0.1:5000/status"):
    r = requests.post(url, data={"interface": iface})
    assert r.status_code == 200
    # print("STATUS OUTPUT:\n", r.text)
    return r.status_code == 200, r.text


def test_apply_and_reset_shaping_flow():
    iface = "enp0s25"

    # Reset and apply shaping to the enp0s25 interface
    ok, _ = send_reset(iface)
    assert ok, "Failed to reset shaping"

    # Apply shaping on eth0
    ok, out = send_apply(iface=iface, rate="10mbit", loss=1, duplicate=2, protocol="tcp")
    assert ok == True
    assert "enp0s25" in out

    # Check status confirms shaping is applied
    ok, status_out = get_status(iface)
    assert ok is True
    assert "htb" in status_out.lower()
    assert "10mbit" in status_out.lower()
    assert "loss 1%" in status_out.lower()
    assert "duplicate 2%" in status_out.lower()

    # Now reset shaping on enp0s25
    ok, out = send_reset(iface=iface)
    assert ok == True
    assert "enp0s25" in out

    # Confirm reset took effect
    ok, status_out = get_status(iface)
    assert ok is True
    assert any(q in status_out.lower() for q in ("noqueue", "pfifo_fast", "fq_codel", "default"))


# @pytest.mark.skip(reason="debugging tests")
def test_applied_delay_effect():
    # Reset existing shaping
    iface = "enp0s25"
    ping_target = "8.8.8.8"

    ok, _ = send_reset(iface)
    assert ok

    ip = get_ip_address(iface)
    print("IP:", ip)

    # Apply shaping with delay
    ok, _ = send_apply(iface=iface, rate="10mbit", loss=0, duplicate=0, protocol="all", delay=2000)
    assert ok
    time.sleep(1)  # Give shaping time to apply

    # Run ping test (check interface delay via ping -I enp0s25 -c 5 8.8.8.8)
    # use ping from iputils-ping, not inetutils-ping (Debian 12)
    result = subprocess.run(
        ["ping", "-I", iface, "-c", "5", ping_target],
        capture_output=True, text=True
    )

    print("PING STDOUT:", result.stdout)
    print("PING STDERR:", result.stderr)
    print("PING return code:", result.returncode)

    assert result.returncode == 0
    assert "avg" in result.stdout
    rtt_line = next((line for line in result.stdout.splitlines() if "rtt" in line), "")
    avg_rtt = float(rtt_line.split("/")[4])
    print(f"Avg RTT: {avg_rtt} ms")

    assert avg_rtt > 150  # Expect delay to show up in RTT

    # Reset again after test
    ok, _ = send_reset("enp0s25")
    assert ok


def extract_bitrate_and_units(iperf_output: str):
    """
    Extract the bitrate and units from iperf3 output focusing on the receiver's summary line.
    Returns: (bitrate_str, units, bitrate_mbit_float)

    Note:
        The receiver's bandwidth line is the better one to measure actual throughput received
        on the shaped interface. The sender line often reports the raw max throughput, not
        limited by shaping on the receiver side.
    """
    bitrate_str = None
    units = None

    # Find the receiver summary line near the end with "receiver" and "bits/sec"
    for line in reversed(iperf_output.splitlines()):
        if "receiver" in line and "bits/sec" in line:
            # Example line:
            # [  5]   0.00-5.53   sec   608 KBytes   901 Kbits/sec                  receiver
            parts = line.split()
            # Usually bitrate is second last token, units last but one
            # Strategy: look for the token that matches bits/sec or similar
            for i, token in enumerate(parts):
                if token.endswith("bits/sec"):
                    # Get the number just before unit
                    bitrate_str = parts[i - 1]
                    units = token
                    break
            if bitrate_str and units:
                break

    if not bitrate_str or not units:
        raise ValueError("No receiver bitrate line found in iperf3 output")

    # Convert to Mbit/sec
    rate = float(bitrate_str)
    units_lower = units.lower()
    if "kbits" in units_lower:
        rate /= 1000
    elif "gbits" in units_lower:
        rate *= 1000
    # else assume Mbits

    return bitrate_str, units, rate


# @pytest.mark.skip(reason="debugging tests")
def test_bandwidth_limit():
    iface = "lo"

    # Reset and apply shaping to the loopback interface
    ok, _ = send_reset(iface)
    assert ok, "Failed to reset shaping"

    ok, _ = send_apply(iface=iface, rate="1mbit", loss=0, duplicate=0, protocol="all", delay=0)
    assert ok, "Failed to apply shaping"

    # Start iperf3 server on localhost
    server = subprocess.Popen(["iperf3", "-s"])
    time.sleep(1)
    print("started iperf3 server")

    print("started iperf3 client to test BW")
    result = subprocess.run(["iperf3", "-c", "127.0.0.1", "-t", "5"], capture_output=True, text=True)
    print("IPERF3 STDOUT:", result.stdout)
    print("IPERF3 STDERR:", result.stderr)

    server.terminate()
    print("terminated iperf3 server")

    server.wait()
    print("waited for child process (iperf3 server) to terminate")

    # Extract reported bitrate
    try:
        bitrate_str, units, rate_mbit = extract_bitrate_and_units(result.stdout)
        print(f"Bitrate: {rate_mbit:.2f} Mbit/sec ({bitrate_str} {units})")
    except Exception:
        raise AssertionError("No bitrate line found in iperf3 output")

    assert rate_mbit < 1.2, f"Expected rate limit around 1 Mbit, got {rate_mbit:.2f} Mbit/sec"

    # Cleanup
    ok, _ = send_reset(iface)
    assert ok, "Failed to reset shaping after test"

