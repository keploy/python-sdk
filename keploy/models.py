
from ast import literal_eval
from typing import Any, Iterable, List, Literal, Optional, Mapping, Sequence

from keploy.constants import ALLOWED_DEPENDENCY_TYPES, ALLOWED_METHODS


def getHostPort(h:str = None, p:int = None):
	host = ''
	suffix = ''
	port = 0

	if not h and not p:
		raise ValueError("Invalid host or port values.")

	if h.startswith(("http:", "https:")):
		raise ValueError("please pass host name without http:// or https://")
		
	host = h
	i = h.find("/")
	if i > 0:
		host = h[:i]
		suffix = h[i+1:]

	port = p or 80
	i = h.find(":")
	if i > 0:
		if p and str(p) != host[i+1:]:
			raise ValueError("2 Ports found. Please pass the port as a function argumet only.")
		port = int(host[i+1:])
		host = host[:i]

	return (host, port, suffix.strip('/'))


class FilterConfig(object):
	def __init__(self, regex: str) -> None:
		self.urlRegex = regex


class AppConfig(object):  
	def __init__(self, name:str=None, host:str=None, port:int=None, delay:int=5, timeout:int=60, filters:FilterConfig=None):
		self.name = name or 'test-keploy'
		self.host = host
		self.port = port
		self.delay = delay
		self.timeout = timeout
		self.filters = filters
		self.suffix = None

		if not host:
			raise ValueError("Host not provided in AppConfig.")

		self.host, self.port, self.suffix = getHostPort(host, port)


PROTOCOL = Literal['https', 'http']
class ServerConfig(object):
	def __init__(self, protocol:PROTOCOL='https', host:str='api.keploy.io', port:int=None, licenseKey:str=''):

		self.protocol = protocol
		self.url = host
		self.port = port
		self.licenseKey = licenseKey
		self.suffix = ''

		if protocol not in ["http", "https"]:
			raise ValueError("Invalid protocol type. Please use from available options.")

		if not licenseKey:
			self.url, self.port, self.suffix = getHostPort(self.url, self.port)	
		

class Config(object):
	def __init__(self, appConfig:AppConfig, serverConfig:ServerConfig) -> None:

		if not isinstance(appConfig, AppConfig):
			raise TypeError("Please provide a valid app configuration.")

		if not isinstance(serverConfig, ServerConfig):
			raise TypeError("Please provide a valid server configuration.")

		self.app = appConfig
		self.server = serverConfig


TYPES= Literal['NO_SQL_DB', 'SQL_DB', 'GRPC', 'HTTP_CLIENT']
class Dependency(object):
	def __init__(self, name:str, type:TYPES, meta:Mapping[str, str]=None, data:List[bytearray]=None) -> None:
		self.name = name
		self.type = type
		self.meta = meta
		self.data = data

		if not type in ALLOWED_DEPENDENCY_TYPES:
			raise TypeError("Please provide a valid dependency type.")


METHODS = Literal['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS', 'TRACE']
class HttpReq(object):
	def __init__(self, method:METHODS=None, proto_major:int=1, proto_minor:int=1, url:str=None, url_params:Mapping[str, str]=None, header:Mapping[str, Sequence[str]]=None, body:str=None) -> None:
		self.method = method
		self.proto_major = proto_major
		self.proto_minor = proto_minor
		self.url = url
		self.url_params = url_params
		self.header = header
		self.body = body

		if method not in ALLOWED_METHODS:
			raise ValueError("{} method is not supported. Please try some other method.".format(self.method))


class HttpResp(object):
	def __init__(self, status_code:int=None, header:Mapping[str, Sequence[str]]=None, body:str=None) -> None:
		self.status_code = status_code
		self.header = header
		self.body = body


class TestCase(object):
	def __init__(self, id:str=None, created:int=None, updated:int=None, captured:int=None,
				 cid:str=None, app_id:str=None, uri:str=None, http_req:HttpReq=None,
				 http_resp: HttpResp=None, deps:Sequence[Dependency]=None, all_keys:Mapping[str, Sequence[str]]=None,
				 anchors:Mapping[str, Sequence[str]]=None, noise:Sequence[str]=None ) -> None:

		#TODO: Need to handle the case when the API response is None instead of [] for deps
		if not deps:
			deps = []

		self.id = id
		self.created = created
		self.updated = updated
		self.captured = captured
		self.cid = cid
		self.app_id = app_id
		self.uri = uri
		self.http_req = http_req
		self.http_resp = http_resp
		self.deps = [Dependency(**dep) if not isinstance(dep, Dependency) else dep for dep in deps]
		self.all_keys = all_keys
		self.anchors = anchors
		self.noise = noise

		if not isinstance(http_req, HttpReq):
			self.http_req = HttpReq(**http_req)

		if not isinstance(http_resp, HttpResp):
			self.http_resp = HttpResp(**http_resp)


class TestCaseRequest(object):
	def __init__(self, captured:int=None, app_id:str=None, uri:str=None, http_req:HttpReq=None, http_resp:HttpResp=None, deps:Iterable[Dependency]=None) -> None:
		self.captured = captured
		self.app_id = app_id
		self.uri = uri
		self.http_req = http_req
		self.http_resp = http_resp
		self.deps = deps

		if not captured:
			raise ValueError("Captured time cannot be empty.")
		
		if not app_id:
			raise ValueError("valid App ID is required to link the TestReq object.")


class TestReq(object):
	def __init__(self, id:str=None, app_id:str=None, run_id:str=None, resp:HttpResp=None) -> None:
		self.id = id
		self.app_id = app_id
		self.run_id = run_id
		self.resp = resp

		if not id:
			raise ValueError("ID is required in the TestReq object.")
		
		if not app_id:
			raise ValueError("valid App ID is required to link the TestReq object.")