"""WSGI entry point for production servers (gunicorn, uwsgi, …).

Example::

    gunicorn -b 0.0.0.0:5001 -w 2 wsgi:application
"""
from app import create_app

application = create_app()
