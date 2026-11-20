import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from docpull import web


@pytest.fixture()
def server(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(tmp_path, ""))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def post(base, body):
    req = urllib.request.Request(base + "/api/run", data=json.dumps(body).encode(), method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_page_and_config(server):
    with urllib.request.urlopen(server + "/") as r:
        assert b"Document Puller" in r.read()
    with urllib.request.urlopen(server + "/api/config") as r:
        assert json.load(r) == {"needs_user_agent": True}


@pytest.mark.parametrize("body, error", [
    ({"company": ""}, "Enter a company."),
    ({"company": "CVNA", "years": 50, "user_agent": "a b@c.com"}, "Years must be between 0.25 and 20."),
    ({"company": "CVNA"}, "Enter your name and work email (the SEC requires it)."),
])
def test_run_validation(server, body, error):
    assert post(server, body) == (400, {"error": error})


def test_unknown_job_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(server + "/api/status/nope")
    assert e.value.code == 404
