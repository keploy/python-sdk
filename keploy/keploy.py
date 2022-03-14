import json
import logging
import http.client
import re
import time
from typing import Iterable, List, Mapping, Optional, Sequence

from keploy.models import Config, Dependency, HttpResp, TestCase, TestCaseRequest, TestReq


class Keploy(object):
    
    def __init__(self, conf:Config) -> None:
        
        logger = logging.getLogger('keploy')
        logger.setLevel(logging.DEBUG)

        self._config = conf
        self._logger = logger
        self._dependencies:Mapping[str, List[Dependency]] = {}
        self._responses:Mapping[str, HttpResp] = {}
        self._client = http.client.HTTPConnection(host=self._config.server.url, port=self._config.server.port)
    
    
    def get_dependencies(self, id: str) -> Optional[Iterable[Dependency]]:
        return self._dependencies.get(id, None)

    
    def get_resp(self, id: str) -> Optional[HttpResp]:
        return self._responses.get(id, None)
    
    
    def put_resp(self, id:str, resp: HttpResp) -> None:
        self._responses[id] = resp
  
    
    def capture(self, request:TestCaseRequest):
        self.put(request)


    def start(self, total:int) -> str:
        headers = {'Content-type': 'application/json', 'key': self._config.server.licenseKey}
        self._client.request("GET", "/regression/start?app={}&total={}".format(self._config.app.name, total), None, headers)
        
        response = self._client.getresponse()
        if response.status != 200:
            self._logger.error("failed to perform start operation.")
            return ""
        
        body = json.loads(response.read().decode())
        if body.get('id', None):
            return body['id']

        return ""


    def end(self, id:str, status:bool) -> None:
        headers = {'Content-type': 'application/json', 'key': self._config.server.licenseKey}
        self._client.request("GET", "/regression/end?id={}&status={}".format(id, status), None, headers)
        
        response = self._client.getresponse()
        if response.status != 200:
            self._logger.error("failed to perform end operation.")
        
        return


    def put(self, rq: TestCaseRequest):
        filters = self._config.app.filter
        match = re.search(filters.urlRegex, rq.uri)
        if match:
            return None

        headers = {'Content-type': 'application/json', 'key': self._config.server.licenseKey}
        bytes_data = json.dumps(rq).encode()
        self._client.request("POST", "/regression/testcase", bytes_data, headers)
        
        response = self._client.getresponse()
        if response.status != 200:
            self._logger.error("failed to send testcase to backend")
        
        body = json.loads(response.read().decode())
        if body.get('id', None):
            self.denoise(body['id'], rq)

    
    def denoise(self, id:str, tcase:TestCaseRequest):
        time.sleep(2.0)
        unit = TestCase(id, captured=tcase.captured, uri=tcase.uri, req=tcase.httpRequest, deps=tcase.deps)
        res = self.simulate(unit)
        if not res:
            self._logger.error("failed to simulate request")
            return
        
        headers = {'Content-type': 'application/json', 'key': self._config.server.licenseKey}
        bin_data = json.dumps(TestReq(id=id, appid=self._config.app.name, runid=None, resp=res)).encode()
        self._client.request("POST", "/regression/denoise", bin_data, headers)
        
        response = self._client.getresponse()
        if response.status != 200:
            self._logger.error("failed to de-noise request to backend")

    
    def simulate(self, test_case:TestCase) -> Optional[HttpResp]:
        self._dependencies[test_case.id] = test_case.deps
        
        heads = test_case.http_req.header
        heads['KEPLOY_TEST_ID'] = test_case.id
        cli = http.client.HTTPConnection()
        cli._http_vsn_str = 'HTTP/{}.{}'.format(test_case.http_req.protoMajor, test_case.http_req.protoMinor)
        cli.request(
            method=test_case.http_req.method,
            url="http://" + self._config.app.host + ":" + self._config.app.port + test_case.http_req.url,
            body=json.dumps(test_case.http_req.body).encode(),
            headers=heads
        )

        response = self.get_resp(test_case.id)
        if not response or response.pop(test_case.id, None):
            self._logger.error("failed loading the response for testcase.")
            return

        self._dependencies.pop(test_case.id, None)
        cli.close()
        
        return response
        

    def check(self, r_id:str, tc: TestCase) -> bool:
        resp = self.simulate(tc)
        if not resp:
            self._logger.error("failed to simulate request on local server")
            return False
        
        headers = {'Content-type': 'application/json', 'key': self._config.server.licenseKey}
        bytes_data = json.dumps(TestReq(id=tc.id, appid=self._config.app.name, runid=r_id, resp=resp)).encode()
        self._client.request("POST", "/regression/test", bytes_data, headers)
        
        response = self._client.getresponse()
        if response.status != 200:
            self._logger.error("failed to de-noise request to backend")

        body = json.loads(response.read().decode())
        if body.get('pass', False):
            return body['pass']
        
        return False
    
    
    def get(self, id:str) -> Optional[TestCase]:
        
        headers = {'Content-type': 'application/json', 'key': self._config.server.licenseKey}
        self._client.request("GET", "/egression/testcase/{}".format(id), None, headers)
        
        response = self._client.getresponse()
        if response.status != 200:
            self._logger.error("failed to get request.")

        body = json.loads(response.read().decode())
        unit = TestCase(**body)
        
        return unit


    def fetch(self, offset:int=0, limit:int=25) -> Optional[Sequence[TestCase]]:
        test_cases = []
        headers = {'Content-type': 'application/json', 'key': self._config.server.licenseKey}
        
        while True:
            
            self._client.request("GET", "/regression/testcase?app={}&offset={}&limit={}".format(self._config.app.name, offset, limit), None, headers)
            response = self._client.getresponse()
            if response.status != 200:
                self._logger.error("failed to get request.")

            body = json.loads(response.read().decode())
            if body:
                test_cases.append(body)
            else:
                break
        
        return test_cases

    def test(self):
        passed = True

        time.sleep(self._config.app.delay)
        cases = self.fetch()
        count = len(cases)

        run_id = self.start(count)
        for case in cases:
            ok = self.check(run_id, case)
            if not ok:
                passed = False
        
        self.end(run_id, passed)

        return passed

