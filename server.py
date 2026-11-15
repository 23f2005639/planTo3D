"""PlanTo3D web app: upload a CAD-exported PDF floor plan -> review the detected plan -> build a walkable 3D house.
Standard library only. Run: python3 server.py [--port 8080] [--host 127.0.0.1] [--cors ORIGIN]
Builds run one at a time in Blender (GPU) in a background thread; progress is polled by the pages.
--cors lets pages on another site call the API from the browser (ORIGIN such as https://example.com, or *).
There is no login: run it on a trusted network, or behind your own backend or proxy."""
import importlib, json, os, queue, re, shutil, subprocess, sys, threading, time, traceback, uuid, zipfile
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS = os.path.join(HERE, "jobs")
WEB = os.path.join(HERE, "web")
THREE = os.path.join(HERE, "node_modules", "three")
BLENDER = shutil.which("blender") or "blender"
MAX_UPLOAD = 60 * 1024 * 1024
BUILD_TIMEOUT = 2 * 3600        # seconds; a high-quality build of a large house takes well under an hour
ID = re.compile(r"^[a-f0-9]{12}$")
sys.path.insert(0, HERE)
import detect, fetch_assets, vector  # noqa: E402

os.makedirs(JOBS, exist_ok=True)
builds = queue.Queue()
lock = threading.Lock()
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".json": "application/json",
         ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".glb": "model/gltf-binary",
         ".wasm": "application/wasm", ".svg": "image/svg+xml", ".pdf": "application/pdf", ".hdr": "application/octet-stream",
         ".zip": "application/zip"}
CORS = sys.argv[sys.argv.index("--cors") + 1] if "--cors" in sys.argv else None


def jdir(jid):
    return os.path.join(JOBS, jid)


def status(jid, **kw):
    p = os.path.join(jdir(jid), "status.json")
    with lock:
        st = json.load(open(p)) if os.path.exists(p) else {}
        st.update(kw, updated=time.time())
        tmp = p + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(st, fh)
        os.replace(tmp, p)
    return st


def read_status(jid):
    p = os.path.join(jdir(jid), "status.json")
    return json.load(open(p)) if os.path.exists(p) else None


# ------------------------------------------------------------------ system check and the materials download
_blender = None                 # Blender's version, read once ("" = not found)
assets_job = dict(running=False, done=0, total=0, failed=[])
export_lock = threading.Lock()


def system():
    """what must be in place before a plan is accepted (shown on the home page, enforced on upload and build)"""
    global _blender
    if _blender is None:
        try:
            out = subprocess.run([BLENDER, "--version"], capture_output=True, text=True, timeout=60).stdout
            _blender = (re.search(r"Blender (\d+\.\d+)", out) or [None, ""])[1]
        except (OSError, subprocess.TimeoutExpired):
            _blender = ""
    checks = dict(
        blender=dict(ok=bool(_blender) and tuple(map(int, _blender.split("."))) >= (4, 2), version=_blender),
        pdf_tools=dict(ok=all(shutil.which(c) for c in ("pdftocairo", "pdftotext", "pdfinfo"))),
        viewer=dict(ok=os.path.isfile(os.path.join(THREE, "build", "three.module.js"))),
        materials=dict(ok=not fetch_assets.missing(), **assets_job))
    return dict(ready=all(c["ok"] for c in checks.values()), **checks)


def fetch_materials():
    def progress(done, total, name):
        assets_job.update(done=done, total=total)
    try:
        assets_job["failed"] = fetch_assets.fetch(progress)
    except Exception as e:
        traceback.print_exc()
        assets_job["failed"] = [str(e)]
    finally:
        assets_job["running"] = False


def export_blend(jid):
    """jobs/<id>/house_blender.zip: the built house as one .blend with every texture packed inside (made once per build)"""
    d = jdir(jid)
    src, out = os.path.join(d, "house_baked.blend"), os.path.join(d, "house_blender.zip")
    if not os.path.exists(src):
        raise ValueError("build the 3D house first")
    with export_lock:
        if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(src):
            tmp = os.path.join(d, "export.blend")
            expr = f"import bpy; bpy.ops.file.pack_all(); bpy.ops.wm.save_as_mainfile(filepath={tmp!r}, copy=True, compress=True)"
            subprocess.run([BLENDER, "-b", src, "--python-expr", expr], check=True, timeout=900, capture_output=True)
            with zipfile.ZipFile(out + ".part", "w", zipfile.ZIP_STORED) as z:       # the .blend is already compressed
                z.write(tmp, "house.blend")
                z.writestr("README.txt", "house.blend - made by PlanTo3D. Open it in Blender 4.2 or newer.\n"
                           "Every texture is packed inside the file. Each storey is one mesh (bake_level_N) with its lamps;\n"
                           "the render engine is Cycles. Textures and models are CC0, from polyhaven.com.\n")
            os.remove(tmp)
            os.replace(out + ".part", out)
    return f"/jobs/{jid}/house_blender.zip"


# ------------------------------------------------------------------ pipeline steps
def run_detect(jid):
    d = jdir(jid)
    pdf = os.path.join(d, "source.pdf")
    try:
        status(jid, stage="detecting", progress=5, message="Reading the drawing")
        n = vector.page_count(pdf)
        for k in range(1, min(n, 6) + 1):        # page images for the review screen (100 dpi = 100/72 px per pt)
            subprocess.run(["pdftocairo", "-png", "-r", "100", "-f", str(k), "-l", str(k), "-singlefile", pdf,
                            os.path.join(d, f"page-{k}")], check=True, timeout=120)
        p1 = vector.read_page(pdf, 1)
        if len(p1["segs"]) < 150:
            raise detect.PlanError("This PDF has almost no vector lines - it looks like a scan or photo. "
                                   "Please export the plan from AutoCAD / your CAD tool as a PDF.")
        importlib.reload(detect)          # pick up detector fixes without restarting the server
        plan = detect.detect(pdf)
        with open(os.path.join(d, "plan.json"), "w") as fh:
            json.dump(plan, fh)
        status(jid, stage="detected", progress=100, message="Plan detected - please review",
               floors=[f["name"] for f in plan["floors"]], scale_source=plan["scale_source"])
    except Exception as e:
        traceback.print_exc()
        known = isinstance(e, detect.PlanError)
        status(jid, stage="failed", message=str(e) if known else "This drawing could not be read. Is it a floor plan exported from a CAD tool?")


def estimate(jid, quality):
    """seconds this build should take: the latest finished build of the same quality on this machine, scaled by the
    number of light maps (one per storey + the site); README timings until a build has been measured"""
    n = len((read_status(jid) or {}).get("floors") or [1]) + 1
    past = [st for st in (read_status(j) for j in os.listdir(JOBS) if ID.match(j))
            if st and st.get("build_seconds") and st.get("quality") == quality]
    if not past:
        return {"draft": 45, "high": 240}[quality] * n
    st = max(past, key=lambda s: s["updated"])
    return st["build_seconds"] * n / (len(st.get("floors") or [1]) + 1)


def builder():
    while True:
        jid, quality = builds.get()
        d = jdir(jid)
        try:
            t0 = time.time()
            status(jid, stage="building", progress=1, message="Starting Blender", started=t0, eta_total=estimate(jid, quality))
            cmd = [BLENDER, "-b", "--factory-startup", "-P", os.path.join(HERE, "blender", "gen.py"), "--",
                   os.path.join(d, "plan.json"), d, "--quality", quality]
            with open(os.path.join(d, "build.log"), "w") as log:
                pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                watchdog = threading.Timer(BUILD_TIMEOUT, pr.kill)      # a hung Blender must not block every later build
                watchdog.start()
                ok = False
                for line in pr.stdout:
                    log.write(line)
                    m = re.match(r"PROGRESS (\d+) (.*)", line)
                    if m:
                        status(jid, progress=int(m.group(1)), message=m.group(2).strip())
                    ok = ok or line.startswith("GEN OK")
                pr.wait()
                watchdog.cancel()
            if not ok or not os.path.exists(os.path.join(d, "house.glb")):
                raise RuntimeError("3D build failed - see build.log")
            for f in ("house.blend", "house.blend1", "house_baked.blend1"):      # keep house_baked.blend only
                if os.path.exists(os.path.join(d, f)):
                    os.remove(os.path.join(d, f))
            status(jid, stage="done", progress=100, message="Ready", quality=quality, build_seconds=round(time.time() - t0))
        except Exception as e:
            traceback.print_exc()
            status(jid, stage="failed", message=str(e))
        finally:
            builds.task_done()


# ------------------------------------------------------------------ http
class H(BaseHTTPRequestHandler):
    server_version = "PlanTo3D/1"

    def log_message(self, fmt, *a):
        if not self.path.startswith("/api/jobs/") or "status" not in self.path:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def cors(self):
        if CORS:
            self.send_header("Access-Control-Allow-Origin", CORS)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename")

    def do_OPTIONS(self):               # the browser's question before a cross-site POST
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def file(self, path, root):
        path = os.path.realpath(path)
        if not path.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(path):
            return self.send(404, {"error": "not found"})
        ext = os.path.splitext(path)[1].lower()
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(size))
        # job outputs change on every rebuild: never let the browser show a stale house
        fresh = ext in (".json", ".html", ".js", ".css") or "/jobs/" in path.replace(os.sep, "/")
        self.send_header("Cache-Control", "no-cache" if fresh else "max-age=3600")
        self.cors()
        self.end_headers()
        with open(path, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile)

    def do_GET(self):
        u = urlparse(self.path)
        p = unquote(u.path)
        if p in ("/", "/index.html"):
            return self.file(os.path.join(WEB, "index.html"), WEB)
        if p.startswith("/web/"):
            return self.file(os.path.join(WEB, p[5:]), WEB)
        if p.startswith("/three/"):
            return self.file(os.path.join(THREE, p[7:]), THREE)
        if p.startswith("/jobs/"):
            parts = p[6:].split("/", 1)
            if len(parts) == 2 and ID.match(parts[0]):
                return self.file(os.path.join(jdir(parts[0]), parts[1]), jdir(parts[0]))
            return self.send(404, {"error": "not found"})
        m = re.match(r"^/api/jobs/([a-f0-9]{12})$", p)
        if m:
            st = read_status(m.group(1))
            if st and st.get("stage") == "building" and st.get("started"):      # counted here: the viewer's clock may differ
                left = st["started"] + st["eta_total"] - time.time()
                st["eta_left"] = max(0, round(min(left, 15) if st.get("progress", 0) >= 92 else left))   # 92 = exporting, the last step
            return self.send(200 if st else 404, st or {"error": "no such job"})
        if p == "/api/system":
            return self.send(200, system())
        if p == "/api/jobs":
            out = []
            for jid in sorted(os.listdir(JOBS), key=lambda j: -os.path.getmtime(jdir(j))):
                st = read_status(jid) if ID.match(jid) else None
                if st:
                    out.append(dict(id=jid, **{k: st.get(k) for k in ("name", "stage", "updated", "floors")}))
            return self.send(200, out[:30])
        self.send(404, {"error": "not found"})

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 <= n <= MAX_UPLOAD:
            raise ValueError("file too large (60 MB max)")
        return self.rfile.read(n)

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            if p == "/api/materials":            # start the Poly Haven download (the home page's button)
                with lock:
                    start = not assets_job["running"]
                    if start:
                        assets_job.update(running=True, done=0, total=0, failed=[])
                if start:
                    threading.Thread(target=fetch_materials, daemon=True).start()
                return self.send(200, {"ok": True})
            if (p == "/api/upload" or p.endswith("/build")) and not system()["ready"]:
                return self.send(503, {"error": "The server is not set up yet: open its home page to see what is missing."})
            if p == "/api/upload":
                data = self.body()
                name = os.path.basename(unquote(self.headers.get("X-Filename") or "plan.pdf"))[:120]
                if not data.startswith(b"%PDF"):
                    return self.send(400, {"error": "Please upload a PDF exported from your CAD tool "
                                                    "(images and DWG are not supported yet)."})
                jid = uuid.uuid4().hex[:12]
                os.makedirs(jdir(jid))
                with open(os.path.join(jdir(jid), "source.pdf"), "wb") as fh:
                    fh.write(data)
                status(jid, name=name, stage="uploaded", progress=0, message="Uploaded", created=time.time())
                threading.Thread(target=run_detect, args=(jid,), daemon=True).start()
                return self.send(200, {"id": jid})
            m = re.match(r"^/api/jobs/([a-f0-9]{12})/(plan|build|export)$", p)
            if m and os.path.isdir(jdir(m.group(1))):
                jid, what = m.groups()
                if what == "export":
                    return self.send(200, {"url": export_blend(jid)})
                st = read_status(jid) or {}
                if st.get("stage") in ("building", "queued"):
                    return self.send(409, {"error": "a build is already running"})
                if what == "plan":
                    plan = json.loads(self.body())
                    if not isinstance(plan, dict) or not isinstance(plan.get("floors"), list) or not plan["floors"]:
                        return self.send(400, {"error": "invalid plan"})
                    with open(os.path.join(jdir(jid), "plan.json"), "w") as fh:
                        json.dump(plan, fh)
                    return self.send(200, {"ok": True})
                q = json.loads(self.body() or b"{}").get("quality", "high")
                q = q if q in ("draft", "high") else "high"
                status(jid, stage="queued", progress=0, message=f"Waiting for the 3D builder ({builds.qsize()} ahead)")
                builds.put((jid, q))
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError) as e:
            self.send(400, {"error": str(e)})
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            traceback.print_exc()
            self.send(500, {"error": "Blender could not write the file"})


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8080
    host = sys.argv[sys.argv.index("--host") + 1] if "--host" in sys.argv else "127.0.0.1"
    for jid in os.listdir(JOBS):           # builds interrupted by a restart
        st = read_status(jid) if ID.match(jid) else None
        if st and st.get("stage") in ("building", "queued", "detecting"):
            status(jid, stage="failed", message="interrupted by a server restart - press Build again")
    threading.Thread(target=builder, daemon=True).start()
    print(f"PlanTo3D on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), H).serve_forever()
