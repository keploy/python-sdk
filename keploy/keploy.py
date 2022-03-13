
import json
import logging
import http.client
import re
from typing import Any, Iterable, List, Mapping, Optional, Sequence

from keploy.models import Config, Dependency, HttpResp, TestCaseRequest


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

    def put(self, rq: TestCaseRequest):
        filters = self._config.app.filter
        match = re.search(filters.urlRegex, rq.uri)
        if match:
            return None

        headers = {'Content-type': 'application/json', "key": self._config.server.licenseKey}
        bytes_data = json.dumps(rq).encode()
        self._client.request("POST", "/regression/testcase", bytes_data, headers)
        
        response = self._client.getresponse()
        if not response.status == 200:
            self._logger.error("failed to send testcase to backend")
        
        body = json.loads(response.read().decode())
        if body.get('id', None):
            self.denoise(body['id'], rq)

    def denoise(id:str, tcase:TestCaseRequest):
        pass

    def test():
        pass

    def start():
        pass

    def end():
        pass

    def simulate():
        pass

    def check():
        pass

    def get():
        pass

    def fetch():
        pass