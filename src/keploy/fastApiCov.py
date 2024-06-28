import coverage
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from .utils import write_dedup

class FastApiCoverageMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        id = request.headers.get('KEPLOY-TEST-ID')
        testSet = request.headers.get('KEPLOY-TEST-SET-ID')
        if id is None:
            response = await call_next(request)
            return response

        cov = coverage.Coverage(cover_pylib=False)
        cov.start()
        response = await call_next(request)
        cov.stop()
        result = cov.get_data()
        write_dedup(result, id, testSet)
        return response
