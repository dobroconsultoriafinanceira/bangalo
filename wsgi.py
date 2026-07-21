# -*- coding: utf-8 -*-
"""Ponto de entrada WSGI (Gunicorn: `gunicorn wsgi:app`)."""
from app import create_app

app = create_app()
