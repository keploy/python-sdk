
from typing import Any, Iterable, List, Literal, Optional, Mapping, Sequence

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


TYPES: Literal['NO_SQL_DB', 'SQL_DB', 'GRPC', 'HTTP_CLIENT']
class Dependency(object):
    def __init__(self, name:str, type:TYPES, metadata:Mapping[str, str], data:List[List[bytearray]] ) -> None:
        self.name = name
        self.type = type
        self.meta = metadata
        self.data = data


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


class HttpResp(object):
    def __init__(self, code:int, header:Mapping[str, Sequence[str]], body:str ) -> None:
        self.code = code
        self.header = header
        self.body = body


class TestCase(object):
	def __init__(self, id:str, created:Optional[int], updated:Optional[int], captured:Optional[int],
				 cid:Optional[str], appid:Optional[str], uri:Optional[str], req:Optional[HttpReq],
				 res: Optional[HttpResp], deps:Optional[Sequence[Dependency]], keys:Optional[Mapping[str, Sequence[str]]],
				 anchor:Optional[Mapping[str, Sequence[str]]], noise:Optional[Sequence[str]]) -> None:
		self.id = id
		self.created = created
		self.updated = updated
		self.captured = captured
		self.c_id = cid
		self.app_id = appid
		self.uri = uri
		self.http_req = req
		self.http_resp = res
		self.deps = deps
		self.all_keys = keys
		self.anchors = anchor
		self.noise = noise


class TestCaseRequest(object):
	def __init__(self, captured:int, appid:str, uri:str, request:HttpReq, response:HttpResp, deps:Iterable[Dependency]) -> None:
		self.captured = captured
		self.app_id = appid
		self.uri = uri
		self.httpRequest = request
		self.httpResponse = response
		self.deps = deps


class TestReq(object):
	def __init__(self, id:str, appid:str, runid:Optional[str], resp:HttpResp) -> None:
		self.id = id
		self.app_id = appid
		self.run_id = runid
		self.resp = resp