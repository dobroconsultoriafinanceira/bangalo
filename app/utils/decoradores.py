# -*- coding: utf-8 -*-
"""Decorators de autorização por role."""
from functools import wraps

from flask import abort
from flask_login import current_user


def role_required(*roles: str):
    """Exige login + uma das roles. `consultoria` é a role master/admin."""

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if current_user.role not in roles:
                abort(403)
            return fn(*args, **kwargs)

        return wrapper

    return decorator
