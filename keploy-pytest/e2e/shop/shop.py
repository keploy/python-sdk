"""The code under test: a shop that prices, reserves and charges through a
provider over HTTP (the standard library's client, one connection a call)."""
import http.client
import json
import os
import urllib.parse

BASE = urllib.parse.urlsplit(os.environ.get("SHOP_PROVIDER") or "http://127.0.0.1:18741")


def call(method, path, body=None):
    conn = http.client.HTTPConnection(BASE.hostname, BASE.port)
    try:
        data = None if body is None else json.dumps(body)
        conn.request(method, path, body=data, headers={} if data is None else {"content-type": "application/json"})
        res = conn.getresponse()
        text = res.read().decode()
    finally:
        conn.close()
    if res.status != 200:
        raise RuntimeError("%s %s: HTTP %d: %s" % (method, path, res.status, text))
    return json.loads(text)


def price(item):
    return call("GET", "/price/" + item)["cents"]


def reserve(item):
    return call("POST", "/reserve", {"item": item})["ok"]


def charge(cents):
    return call("POST", "/charge", {"cents": cents})["status"]
