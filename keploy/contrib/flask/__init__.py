#  BSD 3-Clause License

import copy
import io
from typing import Any
from flask import Flask, request
from keploy.constants import MODE_OFF
import keploy as k
from keploy.contrib.flask.utils import get_request_data
from keploy.models import HttpResp
from keploy.utils import capture_test
from werkzeug import Response

class KFlask(object):

    def __init__(self, keploy:k.Keploy=None, app:Flask=None):
        self.app = app
        self.keploy = keploy

        if not app:
            raise ValueError("Flask app instance not passed, Please initiate flask instance and pass it as keyword argument.")

        if not keploy or k.getMode() == MODE_OFF:
            return

        app.wsgi_app = KeployMiddleware(keploy, app.wsgi_app)


class KeployMiddleware(object):
    def __init__(self, kep, app) -> None:
        self.app = app
        self.keploy = kep
    
    def __call__(self, environ, start_response) -> Any:

        if not self.keploy:
            return self.app(environ, start_response)

        req = {}
        res = {}

        def _start_response(status, response_headers, *args):
            nonlocal req
            nonlocal res
            req = get_request_data(request)
            res['header'] = {key: [value] for key,value in response_headers}
            res['status_code'] = int(status.split(' ')[0])
            return start_response(status, response_headers, *args)
        
        def _end_response(resp_body):
            nonlocal res
            res['body'] = b"".join(resp_body).decode("utf8")
            return [res['body'].encode('utf-8')]
        
        resp = _end_response(self.app(environ, _start_response))
        
        if environ.get("HTTP_KEPLOY_TEST_ID", None):
            self.keploy.put_resp(environ.get('HTTP_KEPLOY_TEST_ID'), HttpResp(**res))
        else:
            capture_test(self.keploy, req, res)
        
        return resp
