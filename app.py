from flask import Flask, render_template, request, redirect, url_for
import subprocess
import re
import os
import time

app = Flask(__name__)


def parse_tc_config(iface):
    try:
        # Get qdisc info
        qdisc_output = stat_cmd(f"tc qdisc show dev {iface}")
        class_output = stat_cmd(f"tc class show dev {iface}")
        filter_output = stat_cmd(f"tc filter show dev {iface}")

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
            print("parse_tc_config: found rate {}".format(config["rate"]))

        # Parse loss and duplicate from qdisc netem output
        # Example line:
        # qdisc netem 10: parent 1:11 limit 1000 loss 10% duplicate 5%
        loss_match = re.search(r"loss (\d+(\.\d+)?)%", qdisc_output)
        if loss_match:
            config["loss"] = loss_match.group(1)
            print("parse_tc_config: found loss {}".format(config["loss"]))

        dupe_match = re.search(r"duplicate (\d+(\.\d+)?)%", qdisc_output)
        if dupe_match:
            config["duplicate"] = dupe_match.group(1)
            print("parse_tc_config: found duplicate {}".format(config["duplicate"]))

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
            print("parse_tc_config: set protocol to {}".format(config["protocol"]))

        # Add interface key for template
        config["interface"] = iface
        print("parse_tc_config: all for this interface {}".format(config["interface"]))

        print("parse_tc_config: returning dict: {}".format(config))
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


def stat_cmd(cmd):
    print(f"Running stat command: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout + result.stderr


def run_cmd(cmd):
    print(f"Running command: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Command failed with code {result.returncode} cmd:{cmd}")
        print(f"stderr: {result.stderr.strip()}")
    return result.returncode == 0


def get_interfaces():
    result = subprocess.run("ip -o link show | awk -F': ' '{print $2}'", shell=True, capture_output=True, text=True)
    return result.stdout.strip().split('\n')


def apply_limit(iface, rate, loss=0, duplicate=0, protocol="all"):
    # Cleanup existing shaping (replaced redundant deletes with delete_limit)
    delete_limit(iface)
    time.sleep(0.1)

    # Add root qdisc
    run_cmd(f"tc qdisc add dev {iface} root handle 1: htb default 30")
    time.sleep(0.1)

    # Add primary class
    run_cmd(f"tc class add dev {iface} parent 1: classid 1:1 htb rate {rate}")
    time.sleep(0.1)

    # Add child classes needed for netem and filters
    run_cmd(f"tc class add dev {iface} parent 1:1 classid 1:10 htb rate {rate}")
    time.sleep(0.05)
    run_cmd(f"tc class add dev {iface} parent 1:1 classid 1:30 htb rate 1000mbit")
    time.sleep(0.05)
    run_cmd(f"tc class add dev {iface} parent 1:1 classid 1:11 htb rate {rate}")
    time.sleep(0.05)

    # Attach netem qdisc for loss and duplicate to 1:11
    run_cmd(f"tc qdisc add dev {iface} parent 1:11 handle 10: netem loss {loss}% duplicate {duplicate}%")

    # Add filters based on protocol
    if protocol == "udp":
        proto_num = 17
        run_cmd(f"tc filter add dev {iface} protocol ip parent 1:0 prio 1 u32 match ip protocol {proto_num} 0xff flowid 1:10")
        run_cmd(f"tc filter add dev {iface} protocol ip parent 1:0 prio 2 u32 match u32 0 0 flowid 1:30")
    elif protocol == "tcp":
        proto_num = 6
        run_cmd(f"tc filter add dev {iface} protocol ip parent 1:0 prio 1 u32 match ip protocol {proto_num} 0xff flowid 1:10")
        run_cmd(f"tc filter add dev {iface} protocol ip parent 1:0 prio 2 u32 match u32 0 0 flowid 1:30")
    else:
        run_cmd(f"tc filter add dev {iface} protocol ip parent 1:0 prio 1 u32 match u32 0 0 flowid 1:10")

    # Setup ingress shaping using ifb0 for both directions
    run_cmd("modprobe ifb numifbs=1")
    if os.system("ip link show ifb0 > /dev/null 2>&1") != 0:
        run_cmd("ip link add ifb0 type ifb")
    run_cmd("ip link set dev ifb0 up")
    time.sleep(0.1)

    run_cmd(f"tc qdisc add dev {iface} handle ffff: ingress || true")

    if protocol == "udp":
        run_cmd(f"tc filter add dev {iface} parent ffff: protocol ip prio 1 u32 match ip protocol 17 0xff action mirred egress redirect dev ifb0")
    elif protocol == "tcp":
        run_cmd(f"tc filter add dev {iface} parent ffff: protocol ip prio 1 u32 match ip protocol 6 0xff action mirred egress redirect dev ifb0")
    else:
        run_cmd(f"tc filter add dev {iface} parent ffff: protocol ip prio 1 u32 match u32 0 0 action mirred egress redirect dev ifb0")

    run_cmd(f"tc qdisc add dev ifb0 root handle 2: htb default 30 || true")
    run_cmd(f"tc class add dev ifb0 parent 2: classid 2:1 htb rate {rate}")
    run_cmd(f"tc class add dev ifb0 parent 2:1 classid 2:10 htb rate {rate}")
    run_cmd(f"tc class add dev ifb0 parent 2:1 classid 2:30 htb rate 1000mbit")

    if protocol in ("udp", "tcp"):
        run_cmd(f"tc filter add dev ifb0 protocol ip parent 2:0 prio 1 u32 match ip protocol {proto_num} 0xff flowid 2:10")
        run_cmd(f"tc filter add dev ifb0 protocol ip parent 2:0 prio 2 u32 match u32 0 0 flowid 2:30")
    else:
        run_cmd(f"tc filter add dev ifb0 protocol ip parent 2:0 prio 1 u32 match u32 0 0 flowid 2:10")


def delete_limit(iface):
    # Delete root qdisc
    run_cmd(f"tc qdisc del dev {iface} root 2>/dev/null || true")
    # Delete ingress qdisc and filters if exist
    if os.system(f"tc qdisc show dev {iface} | grep ingress > /dev/null") == 0:
        run_cmd(f"tc filter del dev {iface} parent ffff: 2>/dev/null || true")
        run_cmd(f"tc qdisc del dev {iface} ingress 2>/dev/null || true")
    # Delete ifb0 qdisc and device if exists
    if os.system("ip link show ifb0 > /dev/null 2>&1") == 0:
        run_cmd("tc qdisc del dev ifb0 root 2>/dev/null || true")
        run_cmd("ip link set dev ifb0 down 2>/dev/null || true")
        run_cmd("ip link delete ifb0 type ifb 2>/dev/null || true")


@app.route("/clear", methods=["POST"])
def clear():
    iface = request.form["interface"]
    # run_cmd(f"tc qdisc del dev {iface} root || true")
    delete_limit(iface)
    return redirect(url_for('index'))


@app.route("/reset", methods=["POST"])
def reset():
    iface = request.form["interface"]
    # output = run_cmd(f"tc qdisc del dev {iface} root 2>&1 || true")
    delete_limit(iface)

    # if "Cannot delete qdisc with handle of zero" in output:
    #     output += f"No custom traffic shaping was applied on {iface}.\n"
    # else:
    output = f"\nTraffic shaping reset on {iface}."

    return render_template("status.html", iface=iface, output=output)


@app.route("/status", methods=["POST"])
def status():
    iface = request.form["interface"]
    output = stat_cmd(f"tc qdisc show dev {iface}")
    output += stat_cmd(f"tc class show dev {iface}")
    output += stat_cmd(f"tc filter show dev {iface}")
    if "noqueue" in output or "pfifo_fast" in output:
        output += "\n\nNote: No traffic shaping is currently configured on this interface."
    return render_template("status.html", iface=iface, output=output)


@app.route("/", methods=["GET", "POST"])
def index():
    interfaces = get_interfaces()
    print("index: current interfaces: {}".format(interfaces))
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
        if "apply" in request.form:
            iface = request.form.get("interface")
            rate = request.form.get("rate")
            loss = request.form.get("loss", "")
            duplicate = request.form.get("duplicate", "")
            protocol = request.form.get("protocol", "all")

            print("index [POST]:  interface {} rate {} loss {} duplicate {} protocol {}"
                  .format(iface, rate, loss, duplicate, protocol))

            # Clear existing qdisc
            # run_cmd(f"tc qdisc del dev {iface} root || true")
            delete_limit(iface)

            # Apply HTB rate limiting
            apply_limit(iface, rate, float(loss), float(duplicate), protocol)

            # # Apply netem for loss and duplicate
            # netem_cmd = f"tc qdisc add dev {iface} parent 1:11 handle 10: netem"
            # if loss:
            #     netem_cmd += f" loss {loss}%"
            # if duplicate:
            #     netem_cmd += f" duplicate {duplicate}%"
            # run_cmd(netem_cmd)

            # Protocol filtering with tc-filter
            if protocol.lower() in ['tcp', 'udp']:
                protocol_number = "6" if protocol.lower() == "tcp" else "17"
                run_cmd(f"tc filter add dev {iface} protocol ip parent 1: prio 1 u32 match ip protocol {protocol_number} 0xff flowid 1:11")

            config = {
                "rate": rate,
                "loss": loss,
                "duplicate": duplicate,
                "protocol": protocol,
                "interface": iface
            }
        elif "interface" in request.form:
            print("index [PUT]:  no apply")

            # Just selected an interface: prefill values from tc
            iface = request.form["interface"]
            config = parse_tc_config(iface)
            config["interface"] = iface
    else:
        # On GET, get interface from query param or default to first
        iface = request.args.get("iface")
        print("index [GET]: requested interface {}".format(iface))

        if iface is None or iface not in interfaces:
            iface = interfaces[0] if interfaces else None

        print("index [GET]: using interface {}".format(iface))

        if iface:
            print("index [GET]: parsing tc to populate interface {} settings".format(iface))
            config = parse_tc_config(iface)
            config["interface"] = iface
        else:
            config = {"rate": "", "loss": "", "duplicate": "", "protocol": "", "interface": ""}

    return render_template("index.html", interfaces=interfaces, output=output, config=config)
