import os
import json

CONFIG_PATH = 'interface_configs.json'

def get_all_interfaces():
    # Replace this with actual interface detection if needed
    return ['lo', 'eth0', 'enp0s25']

def get_interface_config(interface):
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH) as f:
        data = json.load(f)
    return data.get(interface, {})

def save_interface_config(interface, config):
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    else:
        data = {}
    data[interface] = config
    with open(CONFIG_PATH, 'w') as f:
        json.dump(data, f, indent=2)
