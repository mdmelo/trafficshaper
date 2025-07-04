#!/bin/bash

set -x

sudo FLASK_DEBUG=1 FLASK_ENV=development FLASK_APP=app.py /media/mike/SAMSUNG/tcTrafficShape/venv/bin/flask run --host=0.0.0.0
