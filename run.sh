#!/bin/bash

# Activate virtual environment and run the Flask application.
# -u forces unbuffered stdout so background-thread print() statements show
# up in the terminal / tee'd log in real time.
source ./venv/bin/activate
python -u app.py


