from flask import Flask, render_template, request, redirect, url_for
import subprocess
import re

app = Flask(__name__)


def parse_tc_config(iface):
    try:
        # Get qdisc info
        qdisc_output = run_cmd(f"tc qdisc show dev {iface}")
        class_output = run_cmd(f"tc class show dev {iface}")
        filter_output = run_cmd(f"tc filter show dev {iface}")

        # Defaults
        config = {
            "rate": "",
            "loss": "",
            "duplicate": "",
            "protocol": ""
        }

        # Parse rate from class output, look for htb class with rate
        # Example line:
        # class htb 1:11 root rate 1Mbit ceil 1Mbit burst 15Kb
        rate_match = re.search(r"rate (\S+)", class_output)
        if rate_match:
            config["rate"] = rate_match.group(1)

        # Parse loss and duplicate from qdisc netem output
        # Example line:
        # qdisc netem 10: parent 1:11 limit 1000 loss 10% duplicate 5%
        loss_match = re.search(r"loss (\d+(\.\d+)?)%", qdisc_output)
        if loss_match:
            config["loss"] = loss_match.group(1)

        dupe_match = re.search(r"duplicate (\d+(\.\d+)?)%", qdisc_output)
        if dupe_match:
            config["duplicate"] = dupe_match.group(1)

        # Parse protocol from filter output
        # Example line:
        # filter protocol ip parent 1: prio 1 u32 match ip protocol 0x06 0xff flowid 1:11
        if "protocol ip" in filter_output:
            if "0x06" in filter_output:
                config["protocol"] = "tcp"
            elif "0x11" in filter_output:
                config["protocol"] = "udp"
            else:
                config["protocol"] = ""

        # Add interface key for template
        config["interface"] = iface

        return config

    except Exception as e:
        # Log or print exception details to aid debugging
        print(f"Error parsing tc config for {iface}: {e}")
        return {
            "rate": "",
            "loss": "",
            "duplicate": "",
            "protocol": "",
            "interface": iface
        }


def run_cmd(cmd):
    print(f"Running: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout + result.stderr


def get_interfaces():
    result = subprocess.run("ip -o link show | awk -F': ' '{print $2}'", shell=True, capture_output=True, text=True)
    return result.stdout.strip().split('\n')


@app.route("/clear", methods=["POST"])
def clear():
    iface = request.form["interface"]
    output = run_cmd(f"tc qdisc del dev {iface} root || true")
    return redirect(url_for('index'))


@app.route("/reset", methods=["POST"])
def reset():
    iface = request.form["interface"]
    output = run_cmd(f"tc qdisc del dev {iface} root 2>&1 || true")

    if "Cannot delete qdisc with handle of zero" in output:
        output = f"No custom traffic shaping was applied on {iface}.\n"
    else:
        output += f"\nTraffic shaping reset on {iface}."

    return render_template("status.html", iface=iface, output=output)


@app.route("/status", methods=["POST"])
def status():
    iface = request.form["interface"]
    output = run_cmd(f"tc qdisc show dev {iface}")
    output += run_cmd(f"tc class show dev {iface}")
    output += run_cmd(f"tc filter show dev {iface}")
    if "noqueue" in output or "pfifo_fast" in output:
        output += "\n\nNote: No traffic shaping is currently configured on this interface."
    return render_template("status.html", iface=iface, output=output)


@app.route("/", methods=["GET", "POST"])
def index():
    interfaces = get_interfaces()
    output = ""
    config = {
        "rate": "",
        "loss": "",
        "duplicate": "",
        "protocol": "",
        "interface": ""
    }

    # If POST, apply shaping
    if request.method == "POST":
        iface = request.form.get("interface")
        rate = request.form.get("rate")
        loss = request.form.get("loss", "")
        duplicate = request.form.get("duplicate", "")
        protocol = request.form.get("protocol", "")

        # Clear existing qdisc
        run_cmd(f"tc qdisc del dev {iface} root || true")

        # Apply HTB rate limiting
        output += run_cmd(f"tc qdisc add dev {iface} root handle 1: htb default 11")
        output += run_cmd(f"tc class add dev {iface} parent 1: classid 1:1 htb rate {rate}")
        output += run_cmd(f"tc class add dev {iface} parent 1:1 classid 1:11 htb rate {rate}")

        # Apply netem for loss and duplicate
        netem_cmd = f"tc qdisc add dev {iface} parent 1:11 handle 10: netem"
        if loss:
            netem_cmd += f" loss {loss}%"
        if duplicate:
            netem_cmd += f" duplicate {duplicate}%"
        output += run_cmd(netem_cmd)

        # Protocol filtering with tc-filter
        if protocol.lower() in ['tcp', 'udp']:
            protocol_number = "6" if protocol.lower() == "tcp" else "17"
            output += run_cmd(f"tc filter add dev {iface} protocol ip parent 1: prio 1 u32 match ip protocol {protocol_number} 0xff flowid 1:11")

        config = {
            "rate": rate,
            "loss": loss,
            "duplicate": duplicate,
            "protocol": protocol,
            "interface": iface
        }
    else:
        # On GET, get interface from query param or default to first
        iface = request.args.get("iface")
        if iface is None or iface not in interfaces:
            iface = interfaces[0] if interfaces else None

        if iface:
            config = parse_tc_config(iface)
            config["interface"] = iface

    return render_template("index.html", interfaces=interfaces, output=output, config=config)

