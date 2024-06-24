import coverage

from .utils import write_dedup

class DjangoCoverageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        id = request.headers.get('KEPLOY-TEST-ID')
        if id == None:
            return self.get_response(request)
        cov = coverage.Coverage(cover_pylib=False)
        cov.start()
        response = self.get_response(request)
        cov.stop()
        result = cov.get_data()
        write_dedup(result, id)
        return response
    