import json
import logging
import http.client
import re
import threading
import time
from typing import Iterable, List, Mapping, Optional, Sequence
from keploy.mode import getMode
from keploy.constants import MODE_TEST, USE_HTTPS

from keploy.models import Config, Dependency, HttpResp, TestCase, TestCaseRequest, TestReq


class Keploy(object):
    
    def __init__(self, conf:Config) -> None:

        if not isinstance(conf, Config):
            raise TypeError("Please provide a valid keploy configuration.")
        
        logger = logging.getLogger('keploy')
        logger.setLevel(logging.DEBUG)

        self._config = conf
        self._logger = logger
        self._dependencies = {}
        self._responses = {}
        self._client = None

        if self._config.server.protocol == USE_HTTPS:
            self._client =  http.client.HTTPSConnection(host=self._config.server.url, port=self._config.server.port)
        else:
            self._client = http.client.HTTPConnection(host=self._config.server.url, port=self._config.server.port)

        # self._client.connect()

        if getMode() == MODE_TEST:
            t = threading.Thread(target=self.test)
            t.start()
    
    
    def get_dependencies(self, id: str) -> Optional[Iterable[Dependency]]:
        return self._dependencies.get(id, None)

    
    def get_resp(self, t_id: str) -> Optional[HttpResp]:
        return self._responses.get(t_id, None)
    
    
    def put_resp(self, t_id:str, resp: HttpResp) -> None:
        self._responses[t_id] = resp
  
    
    def capture(self, request:TestCaseRequest):
        self.put(request)


    def start(self, total:int) -> Optional[str]:
        try:
            headers = {'Content-type': 'application/json', 'key': self._config.server.licenseKey}
            self._client.request("GET", "/{}/regression/start?app={}&total={}".format(self._config.server.suffix, self._config.app.name, total), None, headers)
            
            response = self._client.getresponse()
            if response.status != 200:
                self._logger.error("Error occured while fetching start information. Please try again.")
                return
            
            body = json.loads(response.read().decode())
            if body.get('id', None):
                return body['id']

            self._logger.error("failed to start operation.")
            return
        except:
            self._logger.error("Exception occured while starting the test case run.")


    def end(self, id:str, status:bool) -> None:
        try:
            headers = {'Content-type': 'application/json', 'key': self._config.server.licenseKey}
            self._client.request("GET", "/{}/regression/end?id={}&status={}".format(self._config.server.suffix, id, status), None, headers)
            
            response = self._client.getresponse()
            if response.status != 200:
                self._logger.error("failed to perform end operation.")
            
            return
        except:
            self._logger.error("Exception occured while ending the test run.")


    def put(self, rq: TestCaseRequest):
        try:
            filters = self._config.app.filters
            if filters:
                match = re.search(filters.urlRegex, rq.uri)
                if match:
                    return None

            headers = {'Content-type': 'application/json', 'key': self._config.server.licenseKey}
            bytes_data = json.dumps(rq, default=lambda o: o.__dict__).encode()
            self._client.request("POST", "/{}/regression/testcase".format(self._config.server.suffix), bytes_data, headers)
            
            response = self._client.getresponse()
            if response.status != 200:
                self._logger.error("failed to send testcase to backend")
            
            body = json.loads(response.read().decode())
            if body.get('id', None):
                self.denoise(body['id'], rq)
        except:
            self._logger.error("Exception occured while storing the request information. Try again.")

    
    def denoise(self, id:str, tcase:TestCaseRequest):
        time.sleep(2.0)
        try:
            unit = TestCase(id, captured=tcase.captured, uri=tcase.uri, req=tcase.httpRequest, deps=tcase.deps)
            res = self.simulate(unit)
            if not res:
                self._logger.error("failed to simulate request")
                return
            
            headers = {'Content-type': 'application/json', 'key': self._config.server.licenseKey}
            bin_data = json.dumps(TestReq(id=id, app_id=self._config.app.name, resp=res), default=lambda o: o.__dict__).encode()
            self._client.request("POST", "/{}/regression/denoise".format(self._config.server.suffix), bin_data, headers)
            
            response = self._client.getresponse()
            if response.status != 200:
                self._logger.error("failed to de-noise request to backend")
        except:
            self._logger.error("Error occured while denoising the test case request. Skipping...")

    
    def simulate(self, test_case:TestCase) -> Optional[HttpResp]:
        try:
            self._dependencies[test_case.id] = test_case.deps
            
            heads = test_case.http_req.header
            heads['KEPLOY_TEST_ID'] = [test_case.id]
            
            cli = http.client.HTTPConnection(self._config.app.host, self._config.app.port)
            cli._http_vsn = int(str(test_case.http_req.proto_major) + str(test_case.http_req.proto_minor))
            cli._http_vsn_str = 'HTTP/{}.{}'.format(test_case.http_req.proto_major, test_case.http_req.proto_minor)
                        
            cli.request(
                method=test_case.http_req.method,
                url=self._config.app.suffix + test_case.http_req.url,
                body=json.dumps(test_case.http_req.body).encode(),
                headers={key: value[0] for key, value in heads.items()}
            )

            # TODO: getting None in case of regular execution. Urgent fix needed.
            response = self.get_resp(test_case.id)
            if not response or not self._responses.pop(test_case.id, None):
                self._logger.error("failed loading the response for testcase.")
                return

            self._dependencies.pop(test_case.id, None)
            cli.close()
            
            return response
        
        except  Exception as e:
            self._logger.exception("Exception occured in simulation of test case with id: %s" %test_case.id)
        

    def check(self, r_id:str, tc: TestCase) -> bool:
        try:
            resp = self.simulate(tc)
            if not resp:
                self._logger.error("failed to simulate request on local server.")
                return False
            
            headers = {'Content-type': 'application/json', 'key': self._config.server.licenseKey}
            bytes_data = json.dumps(TestReq(id=tc.id, app_id=self._config.app.name, run_id=r_id, resp=resp), default=lambda o: o.__dict__).encode()
            self._client.request("POST", "/{}/regression/test".format(self._config.server.suffix), bytes_data, headers)
            
            response = self._client.getresponse()
            if response.status != 200:
                self._logger.error("failed to read response from backend")

            body = json.loads(response.read().decode())
            if body.get('pass', False):
                return body['pass']
            
            return False

        except:
            self._logger.exception("[SKIP] Failed to check testcase with id: %s" %tc.id)
            return False
    

    def get(self, id:str) -> Optional[TestCase]:
        try:
            headers = {'Content-type': 'application/json', 'key': self._config.server.licenseKey}
            self._client.request("GET", "/{}/regression/testcase/{}".format(self._config.server.suffix, id), None, headers)
            
            response = self._client.getresponse()
            if response.status != 200:
                self._logger.error("failed to get request.")

            body = json.loads(response.read().decode())
            unit = TestCase(**body)
        
            return unit
        
        except:
            self._logger.error("Exception occured while fetching the test case with id: %s" %id)
            return


    def fetch(self, offset:int=0, limit:int=25) -> Optional[Sequence[TestCase]]:
        try:
            test_cases = []
            headers = {'Content-type': 'application/json', 'key': self._config.server.licenseKey}
            
            while True:
                self._client.request("GET", "/{}/regression/testcase?app={}&offset={}&limit={}".format(self._config.server.suffix, self._config.app.name, offset, limit), None, headers)
                response = self._client.getresponse()
                if response.status != 200:
                    self._logger.error("Error occured while fetching test cases. Please try again.")
                    return

                body = json.loads(response.read().decode())
                if body:
                    for idx, case in enumerate(body):
                        body[idx] = TestCase(**case)
                    test_cases.extend(body)
                    offset += limit
                else:
                    break
            return test_cases
        
        except:
            self._logger.exception("Exception occured while fetching test cases.")
            return


    def test(self):
        passed = True
        time.sleep(self._config.app.delay)
        
        self._logger.info("Started test operations on the captured test cases.")
        cases = self.fetch()
        count = len(cases)
        
        self._logger.info("Total number of test cases to be checked = %d" %count)
        run_id = self.start(count)
        
        if not run_id:
            return

        self._logger.info("Started with testing...")
        for case in cases:
            ok = self.check(run_id, case)
            if not ok:
                passed = False
        self._logger.info("Finished with testing...")
        
        self._logger.info("Cleaning up things...")
        self.end(run_id, passed)
        
        return passed

