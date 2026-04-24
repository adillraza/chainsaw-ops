"""Backwards-compatible entry point.

This file used to contain the entire application (~1,800 lines). It now just
delegates to the :func:`app.create_app` factory so existing entry points
(``run.sh``, ``deploy.sh``, the systemd unit, ``python app.py``) keep working
unchanged.
"""
from app import bootstrap_database, create_app

application = create_app()
app = application

if __name__ == "__main__":
    bootstrap_database(application)
    application.run(debug=True, host="0.0.0.0", port=5001)
