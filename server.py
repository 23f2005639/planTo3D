"""PlanTo3D web app: upload a CAD-exported PDF floor plan -> review the detected plan -> build a walkable 3D house.
Standard library only. Run: python3 server.py [--port 8080] [--host 127.0.0.1] [--cors ORIGIN]
Builds run one at a time in Blender (GPU) in a background thread; progress is polled by the pages.
--cors lets pages on another site call the API from the browser (ORIGIN such as https://example.com, or *).
There is no login: run it on a trusted network, or behind your own backend or proxy."""
import importlib, json, math, os, posixpath, queue, re, shutil, subprocess, sys, threading, time, traceback, uuid, zipfile
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS = os.path.join(HERE, "jobs")
WEB = os.path.join(HERE, "web")
THREE = os.path.join(HERE, "node_modules", "three")
BLENDER = os.environ.get("BLENDER") or shutil.which("blender") or "blender"      # set BLENDER to its full path if it is not on PATH
MAX_UPLOAD = 200 * 1024 * 1024       # plans are small; recordings sent for MP4 conversion are not
BUILD_TIMEOUT = 2 * 3600        # seconds; a high-quality build of a large house takes well under an hour
ID = re.compile(r"^[a-f0-9]{12}$")
sys.path.insert(0, HERE)
import catalog, detect, fetch_assets, furnish  # noqa: E402

os.makedirs(JOBS, exist_ok=True)
builds = queue.Queue()
lock = threading.RLock()        # reentrant: request handlers hold it around status() to check and set a stage in one step
BUSY = ("uploaded", "detecting", "queued", "building")
FINISHES = ("marble", "wood", "tile", "bathtile", "terracotta", "concrete")     # room.floor: blender/lib.py materials, as in edit.js FINISH
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
export_lock, site_lock = threading.Lock(), threading.Lock()      # the site zip takes a second: it must not wait for a Blender pack


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
        thumbs_job["tried"] = False          # the furniture pictures can be drawn now


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


def three_files():
    """the three.js files the viewer loads: what view.js and walk.js import from three, and what those import in turn"""
    def deps(text, here):
        for s in re.findall(r"\b(?:from|import)\s*['\"]([^'\"]+)['\"]", text):
            if s == "three":
                yield "build/three.module.js"
            elif s.startswith("three/addons/"):
                yield "examples/jsm/" + s[13:]
            elif s.startswith(".") and here is not None:
                yield posixpath.normpath(posixpath.join(here, s))
    todo, seen = [f for w in ("view.js", "walk.js") for f in deps(open(os.path.join(WEB, w)).read(), None)], set()
    while todo:
        f = todo.pop()
        if f not in seen and os.path.isfile(os.path.join(THREE, f)):      # (an example in a doc comment may name a file that is not there)
            seen.add(f)
            todo += deps(open(os.path.join(THREE, f)).read(), posixpath.dirname(f))
    # DRACOLoader fetches its decoder at run time (the WebAssembly one, as every browser with import maps has it)
    return sorted(seen) + ["examples/jsm/libs/draco/draco_wasm_wrapper.js", "examples/jsm/libs/draco/draco_decoder.wasm"]


def export_site(jid):
    """jobs/<id>/house_site.zip: the walkthrough as a static website for any web host, without this server (made once
    per build). Only the house itself leaves the job folder: never the client's PDF, the plan or the logs."""
    d = jdir(jid)
    glb, out = os.path.join(d, "house.glb"), os.path.join(d, "house_site.zip")
    if not os.path.exists(glb):
        raise ValueError("build the 3D house first")
    srcs = [glb, os.path.join(d, "meta.json")] + [os.path.join(WEB, f) for f in ("view.html", "view.js", "walk.js", "style.css")]
    with site_lock:
        if not os.path.exists(out) or os.path.getmtime(out) < max(map(os.path.getmtime, srcs)):     # a new build or a new viewer
            meta = json.load(open(os.path.join(d, "meta.json")))
            house = ["meta.json", "house.glb"] + [os.path.basename(f) for f in (meta.get("lightmaps") or {}).values()]
            # view.html with three.js next to it; view.js finds no ?job= and reads the house from ./house/
            page = open(os.path.join(WEB, "view.html")).read().replace('"/three/', '"./three/')
            with zipfile.ZipFile(out + ".part", "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("index.html", page)
                for f in ("view.js", "walk.js", "style.css"):
                    z.write(os.path.join(WEB, f), f)
                for f in three_files():
                    z.write(os.path.join(THREE, f), "three/" + f)
                for f in house:          # already compressed (Draco, WebP)
                    z.write(os.path.join(d, f), "house/" + f, zipfile.ZIP_STORED)
                z.writestr("README.txt", "3D walkthrough - made by PlanTo3D.\n\n"
                           "Put this folder on any static web host (Netlify, GitHub Pages, your own server) and open index.html.\n"
                           "To try it on your computer, run a web server in this folder, for example\n"
                           "    python3 -m http.server 8000\n"
                           "and open http://localhost:8000/ . Opening index.html straight from the disk does not work:\n"
                           "browsers do not load the viewer's modules from file:// pages.\n\n"
                           "Viewer: three.js (MIT). Textures and models are CC0, from polyhaven.com.\n")
            os.replace(out + ".part", out)
    return f"/jobs/{jid}/house_site.zip"


# ------------------------------------------------------------------ your own furniture models
CUSTOM, THUMBS = os.path.join(fetch_assets.ROOT, "custom"), os.path.join(fetch_assets.ROOT, "thumbs")
thumbs_job = dict(running=False, again=False, tried=False)


def render_thumbs():
    """pictures of every replaceable item as it will be built now (built-in or your model): a small house with all of
    them in a row is built without baking, then photographed. Runs again if a model changed meanwhile."""
    try:
        while True:
            thumbs_job["again"] = False
            os.makedirs(THUMBS, exist_ok=True)
            with open(os.path.join(THUMBS, "plan.json"), "w") as fh:
                json.dump(catalog.preview_plan(), fh)
            subprocess.run([BLENDER, "-b", "--factory-startup", "-P", os.path.join(HERE, "blender", "gen.py"), "--",
                            os.path.join(THUMBS, "plan.json"), THUMBS, "--no-bake"], capture_output=True, timeout=1800)
            subprocess.run([BLENDER, "-b", os.path.join(THUMBS, "house.blend"), "-P", os.path.join(HERE, "blender", "thumbs.py"),
                            "--", THUMBS], capture_output=True, timeout=600)
            if not thumbs_job["again"]:
                break
    except Exception:
        traceback.print_exc()
    finally:
        thumbs_job["running"] = False


def refresh_thumbs():
    with lock:
        if thumbs_job["running"]:
            thumbs_job["again"] = True
            return
        thumbs_job.update(running=True, tried=True)
    threading.Thread(target=render_thumbs, daemon=True).start()


def furniture():
    # drawn once on its own; after that only a new or removed model draws them again (a render that fails must not loop)
    if not thumbs_job["tried"] and not all(os.path.exists(os.path.join(THUMBS, f"{fn}.png")) for fn in catalog.CATALOG):
        refresh_thumbs()
    return dict(rendering=thumbs_job["running"], items=[
        dict(fn=fn, label=label, custom=os.path.exists(os.path.join(CUSTOM, f"{fn}.glb")),
             thumb=os.path.exists(os.path.join(THUMBS, f"{fn}.png"))) for fn, (label, *_) in catalog.CATALOG.items()])


def plan_items(jid):
    """the furniture each floor will get: the review page's own list if it edited that floor, else the rules' choice"""
    plan = json.load(open(os.path.join(jdir(jid), "plan.json")))
    return dict(floors=[furnish.floor_items(plan, f) for f in plan["floors"]], catalog={fn: [label, w, d, kw] for fn, (label, w, d, kw) in catalog.CATALOG.items()})


# items the browser may send: must match gen.CALLS (gen.py runs inside Blender, so it cannot be imported here),
# minus "model", whose argument is an asset file name
ITEM_FNS = set(catalog.CATALOG) | {"shower2", "geyser", "kitchen_run", "chimney", "wall_cab2", "rug", "car",
                                   "outdoor_set", "bathtub", "round_rug"}
WORD = re.compile(r"[\w+-]{0,24}")       # side names such as "-x" and material names; nothing that can be a path


def check_items(plan):
    """furniture lists from the browser go to Blender as function arguments: only known items, plain values"""
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    plain = lambda v: v is None or isinstance(v, bool) or num(v) or (isinstance(v, str) and WORD.fullmatch(v))
    for f in plan["floors"]:
        if not isinstance(f, dict) or "items" not in f:
            continue
        items = f["items"]
        if not isinstance(items, list) or len(items) > 400:
            raise ValueError("invalid furniture list")
        for it in items:
            if not (isinstance(it, dict) and isinstance(it.get("fn"), str) and it["fn"] in ITEM_FNS
                    and isinstance(it.get("args"), list) and len(it["args"]) <= 8 and all(num(v) or isinstance(v, str) and WORD.fullmatch(v) for v in it["args"])
                    and isinstance(it.get("box"), list) and len(it["box"]) == 4 and all(map(num, it["box"]))
                    and isinstance(it.get("kw"), dict) and len(it["kw"]) <= 12
                    and all(WORD.fullmatch(k) and plain(v) for k, v in it["kw"].items())
                    and isinstance(it.get("solid", True), bool) and isinstance(it.get("room", ""), str)):
                raise ValueError("invalid furniture item")


# ------------------------------------------------------------------ pipeline steps
def run_detect(jid):
    d = jdir(jid)
    pdf = os.path.join(d, "source.pdf")
    try:
        status(jid, stage="detecting", progress=5, message="Reading the drawing")
        importlib.reload(detect)          # pick up detector fixes without restarting the server
        plan = detect.detect(pdf)
        for k in sorted({f["page"] for f in plan["floors"]}):     # page images for the review screen (100 dpi = 100/72 px per pt)
            subprocess.run(["pdftocairo", "-png", "-r", "100", "-f", str(k), "-l", str(k), "-singlefile", pdf,
                            os.path.join(d, f"page-{k}")], check=True, timeout=120)
        with open(os.path.join(d, "plan.json"), "w") as fh:
            json.dump(plan, fh)
        status(jid, stage="detected", progress=100, message="Plan detected - please review",
               floors=[f["name"] for f in plan["floors"]], scale_source=plan["scale_source"])
    except Exception as e:
        known = isinstance(e, detect.PlanError)
        if not known:                     # a known one is the drawing's fault, already said in plain words
            traceback.print_exc()
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


def photo_estimate(jid):
    """seconds for the realistic photos: one per room, timed on the last house that had them (40 s each until then)"""
    rooms = sum(1 for f in (json.load(open(os.path.join(jdir(jid), "plan.json")))["floors"]) for r in f["rooms"]
                if r["type"] in PHOTO_ROOMS)
    past = [st for st in (read_status(j) for j in os.listdir(JOBS) if ID.match(j)) if st and st.get("photo_seconds")]
    if not past:
        return rooms * 40
    st = max(past, key=lambda s: s["updated"])
    return rooms * st["photo_seconds"] / max(1, len(st.get("photos") or []))


PHOTO_ROOMS = {"bedroom", "living", "dining", "kitchen", "toilet", "study", "puja", "room", "dress"}   # as in blender/photos.py


def run_blender(cmd, log, on_line):
    """run Blender, copy its output to log, hand every line to on_line; killed after BUILD_TIMEOUT"""
    pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    watchdog = threading.Timer(BUILD_TIMEOUT, pr.kill)      # a hung Blender must not block every later build
    watchdog.start()
    try:
        for line in pr.stdout:
            log.write(line)
            on_line(line)
        pr.wait()
    finally:
        watchdog.cancel()


def builder():
    while True:
        jid, quality, photos = builds.get()
        d = jdir(jid)
        try:
            t0 = time.time()
            shutil.rmtree(os.path.join(d, "photos"), ignore_errors=True)     # photos of the old build would not match
            share = 60 if photos else 100           # % of the progress bar for the build; the photos get the rest
            status(jid, stage="building", progress=1, message="Starting Blender", started=t0, photos=None, want_photos=photos,
                   eta_total=estimate(jid, quality) + (photo_estimate(jid) if photos else 0))
            cmd = [BLENDER, "-b", "--factory-startup", "-P", os.path.join(HERE, "blender", "gen.py"), "--",
                   os.path.join(d, "plan.json"), d, "--quality", quality]
            ok = []

            def gen_line(line):
                m = re.match(r"PROGRESS (\d+) (.*)", line)
                if m:
                    status(jid, progress=int(m.group(1)) * share // 100, message=m.group(2).strip())
                if line.startswith("GEN OK"):
                    ok.append(1)
            with open(os.path.join(d, "build.log"), "w") as log:
                run_blender(cmd, log, gen_line)
            if not ok or not os.path.exists(os.path.join(d, "house.glb")):
                raise RuntimeError("3D build failed - see build.log")
            for f in ("house.blend", "house.blend1", "house_baked.blend1"):      # keep house_baked.blend only
                if os.path.exists(os.path.join(d, f)):
                    os.remove(os.path.join(d, f))
            done = dict(quality=quality, build_seconds=round(time.time() - t0))
            if photos:
                t1, names = time.time(), []

                def photo_line(line):
                    m = re.match(r"PROGRESS (\d+) (.*)", line)
                    if m:
                        status(jid, progress=share + int(m.group(1)) * (100 - share) // 100, message=m.group(2).strip())
                    if line.startswith("PHOTO "):
                        names.append(line.split()[1])
                with open(os.path.join(d, "photos.log"), "w") as log:      # a failed photo still leaves a usable house
                    run_blender([BLENDER, "-b", os.path.join(d, "house_baked.blend"), "-P",
                                 os.path.join(HERE, "blender", "photos.py"), "--", d], log, photo_line)
                done.update(photos=names, photo_seconds=round(time.time() - t1))
            status(jid, stage="done", progress=100, message="Ready", **done)
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
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE")

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
        fresh = ext in (".json", ".html", ".js", ".css", ".png") or "/jobs/" in path.replace(os.sep, "/")
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
        if p.startswith("/examples/"):
            return self.file(os.path.join(HERE, "examples", p[10:]), os.path.join(HERE, "examples"))
        if p.startswith("/thumbs/"):
            return self.file(os.path.join(THUMBS, p[8:]), THUMBS)
        if p == "/api/furniture":
            return self.send(200, furniture())
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
                st["eta_left"] = max(0, round(min(left, 15) if st.get("message") == "exporting model" and not st.get("want_photos") else left))   # the build's last step
            return self.send(200 if st else 404, st or {"error": "no such job"})
        m = re.match(r"^/api/jobs/([a-f0-9]{12})/furniture$", p)
        if m:
            if not os.path.exists(os.path.join(jdir(m.group(1)), "plan.json")):
                return self.send(404, {"error": "no plan yet"})
            try:
                return self.send(200, plan_items(m.group(1)))
            except Exception:
                traceback.print_exc()
                return self.send(500, {"error": "The furniture could not be worked out - see the server log."})
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
            raise ValueError("file too large (200 MB max)")
        return self.rfile.read(n)

    def do_DELETE(self):
        p = urlparse(self.path).path
        m = re.match(r"^/api/jobs/([a-f0-9]{12})$", p)
        if m and os.path.isdir(jdir(m.group(1))):
            with lock:
                if (read_status(m.group(1)) or {}).get("stage") in BUSY:
                    return self.send(409, {"error": "This plan is being read or built. Delete it when that has finished."})
                shutil.rmtree(jdir(m.group(1)))
            return self.send(200, {"ok": True})
        m = re.match(r"^/api/furniture/(\w+)$", p)
        if m and m.group(1) in catalog.CATALOG:               # back to the built-in item
            for f in (os.path.join(CUSTOM, m.group(1) + ".glb"), os.path.join(THUMBS, m.group(1) + ".png")):
                if os.path.exists(f):
                    os.remove(f)
            refresh_thumbs()
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            m = re.match(r"^/api/furniture/(\w+)$", p)
            if m and m.group(1) in catalog.CATALOG:           # your model for this item, as a .glb file
                data = self.body()
                if not data.startswith(b"glTF"):
                    return self.send(400, {"error": "Please upload a .glb file (binary glTF)."})
                os.makedirs(CUSTOM, exist_ok=True)
                with open(os.path.join(CUSTOM, m.group(1) + ".glb"), "wb") as fh:
                    fh.write(data)
                if os.path.exists(os.path.join(THUMBS, m.group(1) + ".png")):
                    os.remove(os.path.join(THUMBS, m.group(1) + ".png"))
                refresh_thumbs()
                return self.send(200, {"ok": True})
            if p == "/api/mp4":                               # a WebM recording in, the same video as MP4 out
                if not shutil.which("ffmpeg"):
                    return self.send(501, {"error": "ffmpeg is not installed on the server"})
                r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                                    "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "pipe:1"],
                                   input=self.body(), capture_output=True, timeout=600)
                if r.returncode or not r.stdout:
                    return self.send(500, {"error": "the recording could not be converted"})
                return self.send(200, r.stdout, "video/mp4")
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
            m = re.match(r"^/api/jobs/([a-f0-9]{12})/(plan|build|export|site)$", p)
            if m and os.path.isdir(jdir(m.group(1))):
                jid, what = m.groups()
                if what == "export":
                    return self.send(200, {"url": export_blend(jid)})
                if what == "site":
                    if (read_status(jid) or {}).get("stage") in BUSY:      # half old, half new files: and it would be kept
                        return self.send(409, {"error": "this plan is being read or built"})
                    return self.send(200, {"url": export_site(jid)})
                req = json.loads(self.body() or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("expected a JSON object")
                with lock:               # checked and set in one step: two quick clicks must not queue two builds
                    if (read_status(jid) or {}).get("stage") in BUSY:
                        return self.send(409, {"error": "this plan is being read or built"})
                    if what == "plan":
                        if not isinstance(req.get("floors"), list) or not req["floors"]:
                            return self.send(400, {"error": "invalid plan"})
                        # both go to Blender as material names / colours: only values it knows
                        rooms = [z for f in req["floors"] if isinstance(f, dict) and isinstance(f.get("rooms"), list)
                                 for z in f["rooms"] if isinstance(z, dict)]
                        if any("floor" in z and z["floor"] not in FINISHES for z in rooms):
                            return self.send(400, {"error": "Unknown floor finish."})
                        wall = req.get("wall_color")
                        if "wall_color" in req and not (isinstance(wall, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", wall)):
                            return self.send(400, {"error": "Wall colour must look like #rrggbb."})
                        check_items(req)
                        with open(os.path.join(jdir(jid), "plan.json"), "w") as fh:
                            json.dump(req, fh)
                        return self.send(200, {"ok": True})
                    q = req.get("quality") if req.get("quality") in ("draft", "high") else "high"
                    status(jid, stage="queued", progress=0, message=f"Waiting for the 3D builder ({builds.qsize()} ahead)")
                    builds.put((jid, q, req.get("photos") is True))
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError) as e:
            self.send(400, {"error": str(e)})
        except Exception:              # anything else: still answer, so the page can say what failed
            traceback.print_exc()
            self.send(500, {"error": "The server could not do that - see its log."})


if __name__ == "__main__":
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8080
    host = sys.argv[sys.argv.index("--host") + 1] if "--host" in sys.argv else "127.0.0.1"
    for jid in os.listdir(JOBS):           # work interrupted by a restart: reading starts again, a build is pressed again
        st = read_status(jid) if ID.match(jid) else None
        if st and st.get("stage") in ("uploaded", "detecting"):
            threading.Thread(target=run_detect, args=(jid,), daemon=True).start()
        elif st and st.get("stage") in BUSY:
            status(jid, stage="failed", message="interrupted by a server restart - press Build again")
    threading.Thread(target=builder, daemon=True).start()
    print(f"PlanTo3D on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), H).serve_forever()
