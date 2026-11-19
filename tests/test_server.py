"""The server's guards, without Blender: files outside the web folders stay hidden, a plan that is being read or built
cannot be changed or deleted, bad request bodies get an answer, furniture from the review page reaches Blender only
as known items with plain values, and a cover sheet before the plan is not taken for a scan.  python3 tests/test_server.py"""
import json, os, shutil, subprocess, sys, tempfile, threading, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [ROOT, os.path.join(ROOT, "examples")]
import make_sample as M  # noqa: E402
import detect, server  # noqa: E402

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

    # furniture: the rules' list until the review page sends its own; an empty list is an empty floor
    jid = job("detected")
    assert call("GET", f"/api/jobs/{jid}/furniture")[0] == 404          # not read yet
    plan = detect.detect(os.path.join(ROOT, "examples", "sample_plan.pdf"))
    json.dump(plan, open(os.path.join(server.jdir(jid), "plan.json"), "w"))
    code, out = call("GET", f"/api/jobs/{jid}/furniture")
    out = json.loads(out)
    assert code == 200 and len(out["floors"]) == len(plan["floors"]) and "bed" in out["catalog"], out
    rules = out["floors"][0]
    assert {"bed", "kitchen_run", "wall_cab2"} <= {it["fn"] for it in rules}
    post = lambda p: call("POST", f"/api/jobs/{jid}/plan", json.dumps(p).encode())[0]
    plan["floors"][0]["items"] = rules                                   # the rules' own items pass as they are
    assert post(plan) == 200
    plan["floors"][0]["items"] = []
    assert post(plan) == 200 and json.loads(call("GET", f"/api/jobs/{jid}/furniture")[1])["floors"][0] == []
    bed = next(it for it in rules if it["fn"] == "bed")
    for bad in (dict(bed, fn="model", args=["../../x", 1, 1, 0]), dict(bed, fn="nope"), dict(bed, args=[1, "../x", 0]),
                dict(bed, args=[1, float("nan"), 0]), dict(bed, box=[0, 1, 2]), dict(bed, kw={"w": [5]}),
                dict(bed, kw={"a/b": 1}), dict(bed, kw={"m": "rug\n"}), dict(bed, kw=None), dict(bed, fn=[]), "bed"):
        plan["floors"][0]["items"] = [bad]
        assert post(plan) == 400, bad
    plan["floors"][0]["items"] = [bed] * 401
    assert post(plan) == 400
    plan["floors"][0]["items"] = [dict(bed, args=[1, 2, 90])]
    assert post(plan) == 200
    plan["furnish"] = False
    assert post(plan) == 200 and json.loads(call("GET", f"/api/jobs/{jid}/furniture")[1])["floors"] == [[]] * len(plan["floors"])
    print("ok   server: furniture lists, item checks")

    # a title sheet with no drawing before the plan: the plan on page 2 is found, and its page is the one drawn
    cover = os.path.join(server.JOBS, "cover.pdf")
    open(cover, "wb").write(M.pdf_page(b""))
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
