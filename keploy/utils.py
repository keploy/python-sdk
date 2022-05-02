from keploy.keploy import Keploy
from keploy.models import Dependency, HttpReq, HttpResp, TestCaseRequest
import time

def capture_test(k, reqst, resp):
    
    deps = [ Dependency('demo_dep', 'HTTP_CLIENT', {}, None), ]

    test = TestCaseRequest(
        captured=int(time.time()),
        app_id=k._config.app.name,
        uri=reqst['uri'],
        http_req=HttpReq(
            method=reqst['method'],
            proto_major=reqst['proto_major'],
            proto_minor=reqst['proto_minor'],
            url=reqst['url'],
            url_params=reqst['params'],
            header=reqst['header'],
            body=reqst['body']
        ),
        http_resp=HttpResp(
            status_code=resp['status_code'],
            header=resp['header'],
            body=resp['body']
        ),
        deps=deps
    )

    k.capture(test)

    return