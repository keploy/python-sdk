import coverage
from werkzeug.wrappers import Request
from .utils import write_dedup

class FlaskCoverageMiddleware:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        request = Request(environ)
        id = request.headers.get("Keploy-Test-Id")

        if id == None:
            return self.app(environ, start_response)
        cov = coverage.Coverage(cover_pylib=False)
        cov.start()
        response = self.app(environ, start_response)
        cov.stop()
        result = cov.get_data()
        write_dedup(result, id)
        return response
