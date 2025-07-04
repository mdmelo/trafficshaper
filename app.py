from flask import Flask, render_template, request, redirect, url_for
import subprocess
import re
import os
import time

app = Flask(__name__)


# 1. qdisc (Queueing Discipline)
# Manages how packets are queued and scheduled for transmission on an interface.
#     Root qdisc sits directly on a network device.
#     Can be classless (e.g., pfifo, fq_codel) or classful (e.g., htb, tbf).
#     Implements policies like shaping (rate limiting), prioritization, and delay.
#
# 2. class
# Defines subdivisions under a classful qdisc to allow hierarchical control.
#     Classes inherit and refine the parent qdisc’s behavior.
#     Used to divide traffic (e.g., by type or source) and apply different rates or priorities.
#     Example: htb can have multiple classes for different bandwidth limits.
#
# 3. filter
# Directs packets to a specific class within a qdisc hierarchy based on packet attributes.
#     Match rules based on IP, ports, protocol, fwmark, etc.
#     Essential for traffic classification and routing to proper queues/classes.

# example
#     qdisc htb 1: root refcnt 2 r2q 10 default 0x30 direct_packets_stat 0 direct_qlen 1000
#     qdisc netem 10: parent 1:11 limit 1000 loss 5% duplicate 15%
#     qdisc ingress ffff: parent ffff:fff1 ----------------
#     class htb 1:11 parent 1:1 leaf 10: prio 0 rate 1Gbit ceil 1Gbit burst 1375b cburst 1375b
#     class htb 1:1 root rate 1Gbit ceil 1Gbit burst 1375b cburst 1375b
#     class htb 1:10 parent 1:1 prio 0 rate 1Gbit ceil 1Gbit burst 1375b cburst 1375b
#     class htb 1:30 parent 1:1 prio 0 rate 1Gbit ceil 1Gbit burst 1375b cburst 1375b
#     filter parent 1: protocol ip pref 1 u32 chain 0
#     filter parent 1: protocol ip pref 1 u32 chain 0 fh 800: ht divisor 1
#     filter parent 1: protocol ip pref 1 u32 chain 0 fh 800::800 order 2048 key ht 800 bkt 0 *flowid 1:10 not_in_hw
#       match 00000000/00000000 at 0

# qdisc setup
#     htb 1: root — Hierarchical Token Bucket qdisc as the root, shaping at 1 Gbit.
#     netem 10: on class 1:11 — Simulates 5% packet loss + 15% duplication.
#     ingress ffff: — Placeholder for ingress filtering; currently unused.
#
# classes
#     1:1 — Root class, 1 Gbit rate/ceiling.
#     1:10 — Class for matched traffic, 1 Gbit.
#     1:11 — Same as above, but also has netem (loss/dup).
#     1:30 — Default class (from default 0x30 = 48 decimal = 0x30).
#
# filter
#     Matches all IPv4 traffic (match 00000000/00000000 at 0)
#     Sends it to class 1:10 (flowid 1:10)
#
# Summary
# All IPv4 packets are classified into 1:10, which shares a 1 Gbit pipe with two other unused classes.
#  1:11 is configured with netem, but unused by current filters. Default fallback is 1:30

#    HTB (Hierarchical Token Bucket)  Bandwidth shaping and sharing.
#        Enforces rate limits (minimum and maximum bandwidth).
#        Supports class hierarchy (parent/child classes).
#        Good for dividing bandwidth across multiple traffic types.
#
#        rate: guaranteed bandwidth.
#        ceil: maximum bandwidth.
#        burst: temporary bandwidth allowance for short spikes.
#
#    netem (Network Emulator) Simulates poor network conditions.
#        Adds artificial delay, packet loss, duplication, corruption, etc.
#        Useful for testing how applications behave under bad network conditions.

#    Class Identifiers
#    Format: major:minor
#        major: ID of the parent qdisc (e.g., 1: from htb 1:).
#        minor: ID of the specific class under that qdisc.
#
#    So in class htb 1:10:
#        1: means it's part of qdisc htb 1:
#        10 is the class ID (you choose this)
#
#    Rules:
#        You choose the minor number (:1, :10, :11, :30...), but they must be unique under the same major.
#        Use hex or decimal (e.g., 0x30 == 48)
#
#        Conventionally:
#            1:1 is often the root class.
#            Others (1:10, 1:11, etc.) are leaf or child classes.

# IFB (Intermediate Functional Block) - virtual device to allow application of egress-style shaping to ingress traffic.
#
# The IFB device is used to redirect ingress traffic so that it can be shaped like egress traffic.
# Linux tc can shape egress natively, but can't shape ingress directly (only drop it).
# So you redirect ingress to an IFB device, then shape it there using normal egress tools (htb, netem, etc.).
#
# How it works:
#    Create and bring up an IFB interface (e.g., ifb0).
#    Use tc qdisc add dev eth0 ingress + tc filter ... action mirred egress redirect dev ifb0 to redirect incoming packets.
#    Apply egress shaping qdiscs (e.g., htb, netem) on ifb0.
#
# mirred = "Mirror/Redirect" action - is a tc action module that can mirror packets (copy and send to
# another interface) or redirect packets (move to another interface), commonly used to enable ingress shaping via IFB.



#                       +-----------------+
#                       |  Physical NIC   | eth0
#                       +--------+--------+
#                                |   ingress
#                                ▼
#                     [tc ingress qdisc on eth0]
#                   ┌──────────────────────────────┐
#                   │ tc filter → mirred redirect  │
#                   │   action                     │
#                   └────────────┬─────────────────┘
#                                ▼
#                   +------------------------------+
#                   |        IFB vNIC (ifb0)       |
#                   +-----------+------------------+
#                                |
#                                ▼
#                    [tc egress qdisc on ifb0]
#                      (htb, netem, filters…)
#                                |
#                                ▼
#                      Shaped ingress traffic
#
#                               /\
#                               ||
#                               || egress
#                               ||
#                               \/
#                       +-----------------+
#                       |   Physical NIC  | eth0
#                       +-----------------+
#                                |
#                      [tc egress qdisc on eth0]
#                            (htb, netem…)


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
            "protocol": "",
            "delay": "",
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

        delay_match = re.search(r"delay (\d+)(?:ms)?", qdisc_output)
        if delay_match:
            config["delay"] = delay_match.group(1)
        else:
            config["delay"] = ""

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
            "delay": "",
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


def apply_limit(iface, rate, loss=0, duplicate=0, protocol="all", delay=0):
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
    # Build netem command with delay, loss, duplicate
    netem_cmd = f"tc qdisc add dev {iface} parent 1:11 handle 10: netem"
    if delay and float(delay) > 0:
        netem_cmd += f" delay {delay}ms"
    if loss and float(loss) > 0:
        netem_cmd += f" loss {loss}%"
    if duplicate and float(duplicate) > 0:
        netem_cmd += f" duplicate {duplicate}%"
    run_cmd(netem_cmd)

    # Add filters based on protocol
    if protocol == "udp":
        proto_num = 17
        run_cmd(f"tc filter add dev {iface} protocol ip parent 1:0 prio 1 u32 match ip protocol {proto_num} 0xff flowid 1:11")
        run_cmd(f"tc filter add dev {iface} protocol ip parent 1:0 prio 2 u32 match u32 0 0 flowid 1:30")
    elif protocol == "tcp":
        proto_num = 6
        run_cmd(f"tc filter add dev {iface} protocol ip parent 1:0 prio 1 u32 match ip protocol {proto_num} 0xff flowid 1:11")
        run_cmd(f"tc filter add dev {iface} protocol ip parent 1:0 prio 2 u32 match u32 0 0 flowid 1:30")
    else:
        run_cmd(f"tc filter add dev {iface} protocol ip parent 1:0 prio 1 u32 match u32 0 0 flowid 1:11")

    # Setup ingress shaping using ifb0 for both directions
    run_cmd("modprobe ifb numifbs=1")
    if os.system("ip link show ifb0 > /dev/null 2>&1") != 0:
        run_cmd("ip link add ifb0 type ifb")
    run_cmd("ip link set dev ifb0 up")
    time.sleep(0.1)

    run_cmd(f"tc qdisc add dev {iface} handle ffff: ingress || true")

    # mirred egress redirect ifb0: send the packet out of ifb0, where it can be shaped.
    # used with tc filter to match and redirect ingress traffic.
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
    error_msg = ""
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
            delay = request.form.get("delay", "0")

            print("index [POST]:  interface {} rate {} loss {} duplicate {} protocol {}"
                  .format(iface, rate, loss, duplicate, protocol))

            try:
                # Clear existing qdisc
                # run_cmd(f"tc qdisc del dev {iface} root || true")
                delete_limit(iface)

                # Apply HTB rate limiting
                apply_limit(iface, rate, float(loss), float(duplicate), protocol, float(delay))

            except Exception as e:
                error_msg = f"Error applying settings to interface '{iface}': {str(e)}"

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
                "delay": delay,
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

    return render_template("index.html", interfaces=interfaces, output=output, config=config, error_msg=error_msg)
