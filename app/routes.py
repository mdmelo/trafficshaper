from flask import render_template, request, redirect, url_for, flash
from . import app
from .utils import get_all_interfaces, get_interface_config, save_interface_config

@app.route('/', methods=['GET', 'POST'])
def index():
    interfaces = get_all_interfaces()
    selected_interface = request.args.get('interface') or (interfaces[0] if interfaces else None)

    if request.method == 'POST':
        selected_interface = request.form['interface']
        config = {
            'rate_limit': request.form.get('rate_limit', ''),
            'packet_loss': request.form.get('packet_loss', ''),
            'packet_duplication': request.form.get('packet_duplication', ''),
            'protocol': request.form.get('protocol', 'All')
        }
        save_interface_config(selected_interface, config)
        flash(f"Settings saved for {selected_interface}", "success")
        return redirect(url_for('index', interface=selected_interface))

    current_config = get_interface_config(selected_interface) if selected_interface else {}
    config_defaults = {
        'rate_limit': '',
        'packet_loss': '',
        'packet_duplication': '',
        'protocol': 'All'
    }
    if current_config:
        config_defaults.update(current_config)

    return render_template('index.html',
                           interfaces=interfaces,
                           selected_interface=selected_interface,
                           config=config_defaults)
