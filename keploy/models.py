
from typing import Any, List, Literal, Optional
from collections.abc import Mapping, Sequence


class FilterConfig(object):
	def __init__(self, regex: str) -> None:
		self.urlRegex = regex

class AppConfig(object):  
	def __init__(self, name:str, host:str, port:int, delay:int, timeout:int, filter:FilterConfig):
		self.name = name
		self.host = host
		self.port = port
		self.delay = delay
		self.timeout = timeout
		self.filter = filter


class ServerConfig(object):
	def __init__(self, host:str, port:Optional[int], licenseKey:str):
		self.url = host
		self.port = port
		self.licenseKey = licenseKey


class Config(object):
    def __init__(self, appConfig:AppConfig, serverConfig:ServerConfig) -> None:
        self.app = appConfig
        self.server = serverConfig


class TestCaseRequest(object):
    def __init__(self, captured:int, appID:str, uri:str, request:Any, response:Any) -> None:
        self.captured = captured
        self.appId = appID
        self.uri = uri
        self.httpRequest = request
        self.httpResponse = response


TYPES: Literal['NO_SQL_DB', 'SQL_DB', 'GRPC', 'HTTP_CLIENT']
class Dependency(object):
    def __init__(self, name:str, type:TYPES, metadata:Mapping[str, str], data:List[List[bytearray]] ) -> None:
        self.name = name
        self.type = type
        self.meta = metadata
        self.data = data


class HttpResp(object):
    def __init__(self, code:int, header:Mapping[str, Sequence[str]], body:str ) -> None:
        self.code = code
        self.header = header
        self.body = body


METHODS = Literal['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS', 'TRACE']
class HttpReq(object):
	def __init__(self, method:METHODS, major:int, minor:int, url:str, params:Mapping[str, str], header:Mapping[str, Sequence[str]], body:str ) -> None:
		self.method = method
		self.protoMajor = major
		self.protoMinor = minor
		self.url = url
		self.urlParams = params
		self.header = header
		self.body = body
