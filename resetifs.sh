#!/bin/bash

set -e

# Function to reset a given network interface
reset_interface() {
    local iface="$1"

    echo "Resetting interface: $iface"

    # Bring the interface down and up
    sudo ip link set "$iface" down
    sudo ip addr flush dev "$iface"
    sudo ip link set "$iface" up

    # Remove all traffic control rules
    sudo tc qdisc del dev "$iface" root || true
    sudo tc qdisc del dev "$iface" ingress || true

    echo "$iface reset complete."
}

show_interface() {
    local iface="$1"

    echo "State interface: $iface"

    sudo tc qdisc show dev "$iface"
    sudo tc class show dev "$iface"
    sudo tc filter show dev "$iface"
}

# Reset loopback and Ethernet
reset_interface lo
reset_interface enp0s25

# Show state of loopback and Ethernet
show_interface lo
show_interface enp0s25

