"""The server's guards, without Blender: files outside the web folders stay hidden, a plan that is being read or built
cannot be changed or deleted, bad request bodies get an answer, and a cover sheet before the plan is not taken for a
scan.  python3 tests/test_server.py"""
import json, os, shutil, subprocess, sys, tempfile, threading, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import server  # noqa: E402

server.JOBS = tempfile.mkdtemp()          # never touch the real jobs folder
httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{httpd.server_port}"


def call(method, path, body=None):
    req = urllib.request.Request(URL + path, data=body, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def job(stage):
    jid = os.urandom(6).hex()
    os.makedirs(server.jdir(jid))
    server.status(jid, stage=stage)
    return jid


try:
    assert call("GET", "/web/..%2fserver.py")[0] == 404
    assert call("GET", "/jobs/" + job("done") + "/..%2f..%2fserver.py")[0] == 404
    busy = job("detecting")
    assert call("DELETE", f"/api/jobs/{busy}")[0] == 409 and os.path.isdir(server.jdir(busy))
    assert call("POST", f"/api/jobs/{busy}/plan", b'{"floors": [{}]}')[0] == 409
    done = job("done")
    assert call("POST", f"/api/jobs/{done}/plan", b"[]")[0] == 400
    assert call("POST", f"/api/jobs/{done}/plan", b"not json")[0] == 400
    assert call("POST", f"/api/jobs/{done}/plan", b'{"floors": [{"name": "G"}]}')[0] == 200
    assert json.load(open(os.path.join(server.jdir(done), "plan.json")))["floors"][0]["name"] == "G"
    assert call("POST", f"/api/jobs/{done}/export")[0] == 400           # nothing built yet
    assert call("DELETE", f"/api/jobs/{done}")[0] == 200 and not os.path.exists(server.jdir(done))
    print("ok   server: hidden files, busy plans, bad bodies")

    # a title sheet with no drawing before the plan: the plan on page 2 is found, and its page is the one drawn
    cover = os.path.join(server.JOBS, "cover.pdf")
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>"]
    pdf, offs = b"%PDF-1.4\n", []
    for k, o in enumerate(objs, 1):
        offs.append(len(pdf))
        pdf += b"%d 0 obj%s endobj\n" % (k, o)
    xref = len(pdf)
    pdf += b"xref\n0 4\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offs)
    pdf += b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % xref
    open(cover, "wb").write(pdf)
    jid = job("uploaded")
    subprocess.run(["pdfunite", cover, os.path.join(ROOT, "examples", "sample_plan.pdf"),
                    os.path.join(server.jdir(jid), "source.pdf")], check=True)
    server.run_detect(jid)
    st = server.read_status(jid)
    assert st["stage"] == "detected", st
    assert sorted(os.listdir(server.jdir(jid))) == ["page-2.png", "plan.json", "source.pdf", "status.json"]
    shutil.copy(cover, os.path.join(server.jdir(jid), "source.pdf"))
    server.run_detect(jid)
    assert "scan" in server.read_status(jid)["message"]
    print("ok   server: a cover sheet before the plan, a PDF without lines")
finally:
    httpd.shutdown()
    shutil.rmtree(server.JOBS)
