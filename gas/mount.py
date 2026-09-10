import re


def normalize_base_path(value):
    """Accept an absolute URL path, never a URL or query string."""
    if value in ('', '/'):
        return ''
    if not isinstance(value, str) or not re.fullmatch(r'(?:/[A-Za-z0-9._~-]+)+/?', value):
        raise RuntimeError('APP_BASE_PATH muss ein absoluter Pfad wie /utilmanager sein.')
    path = value.rstrip('/')
    if any(segment in ('.', '..') for segment in path.split('/')):
        raise RuntimeError('APP_BASE_PATH darf keine . oder .. Pfadsegmente enthalten.')
    return path


class BasePathMiddleware:
    """Support proxies that preserve the prefix as well as those that strip it."""

    def __init__(self, app, base_path):
        self.app = app
        self.base_path = base_path

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        if path == self.base_path or path.startswith(self.base_path + '/'):
            environ['PATH_INFO'] = path[len(self.base_path):]
        environ['SCRIPT_NAME'] = self.base_path
        return self.app(environ, start_response)
