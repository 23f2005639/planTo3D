"""Cinematic dusk tour of a built house (house.blend from gen.py): blue-hour sky, warm interior light with LED coves,
one continuous camera glide through every room (route = tour.json), rendered frame by frame with
heat locks on the GPU and CPU.  Resumable: frames already on disk are skipped.

blender -b JOB/house.blend -P blender/cinema.py -- TOUR.json OUTDIR [--stills t1,t2,..] [--shots SHOTS.json] [--save OUT.blend] [--frames] [--res 1920x1080]
TOUR.json: {"fps": 30, "eye": 5.1, "speed": 1.9, "keys": [{"p": [x, y, z], "t": [x, y, z], "hold": s, "room": "..."}]}
(feet; 'hold' = seconds spent turning from the previous target to this one without moving)"""
import bmesh, bpy, glob, json, math, os, subprocess, sys, time
from mathutils import Matrix, Vector

argv = sys.argv[sys.argv.index("--") + 1:]
TOUR, OUT = json.load(open(argv[0])), argv[1]
os.makedirs(OUT, exist_ok=True)
F = 0.3048
HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "..", "assets")
sc = bpy.context.scene
FPS = TOUR.get("fps", 30)
opt = lambda k, d: type(d)(argv[argv.index(k) + 1]) if k in argv else d
RES = [int(v) for v in (argv[argv.index("--res") + 1] if "--res" in argv else "1920x1080").split("x")]

# ------------------------------------------------------------------ path: Catmull-Rom through the keys, timed by length
keys = TOUR["keys"]
times = [0.0]
for a, b in zip(keys, keys[1:]):
    d = (Vector(b["p"]) - Vector(a["p"])).length
    da, db = Vector(a["t"]) - Vector(a["p"]), Vector(b["t"]) - Vector(b["p"])
    ang = da.angle(db, 0.0) if da.length and db.length else 0.0
    turn = max(0.5, ang / (math.pi / 2) * TOUR.get("turn_s", 1.6))    # seconds: slow, even pans
    times.append(times[-1] + max(d / TOUR.get("speed", 1.6), b.get("hold", 0.0) * TOUR.get("hold_k", 1.4), turn))
DUR = times[-1]


def _cr(p0, p1, p2, p3, u):
    return 0.5 * ((2 * p1) + (-p0 + p2) * u + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u * u + (-p0 + 3 * p1 - 3 * p2 + p3) * u ** 3)


def at(t, field):
    t = min(max(t, 0.0), DUR)
    i = max(0, min(len(times) - 2, next((k for k in range(len(times) - 1) if times[k + 1] >= t), len(times) - 2)))
    u = (t - times[i]) / max(times[i + 1] - times[i], 1e-6)
    u = u * u * (3 - 2 * u) if keys[i].get("hold") or keys[i + 1].get("hold") else u   # settle into turns
    P = [Vector(keys[max(0, min(len(keys) - 1, j))][field]) for j in (i - 1, i, i + 1, i + 2)]
    return _cr(*P, u)


def room_at(t):
    i = max(k for k in range(len(times)) if times[k] <= t + 1e-6)
    return next((keys[k]["room"] for k in range(i, -1, -1) if keys[k].get("room")), "")


# ------------------------------------------------------------------ look: dusk sky, warm interiors
sky = os.path.join(ASSETS, "hdri", "qwantani_dusk_2_puresky.hdr")
wn = sc.world.node_tree
env = next(n for n in wn.nodes if n.type == "TEX_ENVIRONMENT")
env.image = bpy.data.images.load(sky)
wn.nodes["Background"].inputs["Strength"].default_value = TOUR.get("sky", 0.3)
mp = wn.nodes.new("ShaderNodeMapping")
tc = wn.nodes.new("ShaderNodeTexCoord")
wn.links.new(tc.outputs["Generated"], mp.inputs["Vector"])
wn.links.new(mp.outputs["Vector"], env.inputs["Vector"])
mp.inputs["Rotation"].default_value[2] = math.radians(TOUR.get("sky_rot", 140))
for o in list(sc.objects):
    if o.type == "LIGHT":                       # the bake's sun and lamps: replaced below
        bpy.data.objects.remove(o)


def light(name, kind, loc, energy, color, size=None, rot=None, coll=sc.collection, **kw):
    L = bpy.data.lights.new(name, kind)
    L.energy, L.color = energy, color
    if size is not None:
        L.size = size
    for k, v in kw.items():
        setattr(L, k, v)
    o = bpy.data.objects.new(name, L)
    o.location = [v * F for v in loc]
    if rot:
        o.rotation_euler = [math.radians(a) for a in rot]
    coll.objects.link(o)
    return o


WARM, LED = (1.0, 0.72, 0.47), (1.0, 0.66, 0.38)
H = TOUR.get("ceiling", 10.0)
plan = json.load(open(TOUR["plan"]))
sys.path.insert(0, os.path.join(HERE, ".."))
from furnish import INDOOR  # noqa: E402
led = bpy.data.materials.new("cove_led")
led.use_nodes = True
em = led.node_tree.nodes.new("ShaderNodeEmission")
em.inputs["Color"].default_value = (*LED, 1)
em.inputs["Strength"].default_value = TOUR.get("cove", 6.0)
led.node_tree.links.new(em.outputs[0], led.node_tree.nodes["Material Output"].inputs["Surface"])
COVES = []
for f in plan["floors"]:
    for z in f["rooms"]:
        if z["type"] not in INDOOR:
            continue
        for x0, x1, y0, y1 in z["rects"]:
            w, d = x1 - x0, y1 - y0
            if w * d < 8:
                continue
            # soft ceiling glow + a warm fill: an area light the size of most of the room
            toured = (y0 + y1) / 2 < TOUR.get("tour_y_max", 1e9)     # the other unit is only seen from outside
            Lo = light("room", "AREA", ((x0 + x1) / 2, (y0 + y1) / 2, H - 0.3), TOUR.get("room_w", 6.0) * w * d * F * F,
                       WARM, shape="RECTANGLE", size=w * 0.7 * F, size_y=d * 0.7 * F)
            Lo.data.use_shadow = True               # (without shadows it leaks through roofs and walls)
            if toured:      # LED cove wash: a long thin light along each wall pointing at the ceiling
                for cx, cy, lx, ly in (((x0 + x1) / 2, y0 + 0.5, w, 0.15), ((x0 + x1) / 2, y1 - 0.5, w, 0.15),
                                       (x0 + 0.5, (y0 + y1) / 2, 0.15, d), (x1 - 0.5, (y0 + y1) / 2, 0.15, d)):
                    c = light("cove", "AREA", (cx, cy, H - 0.55), TOUR.get("cove_w", 2.2) * max(lx, ly) * F, LED,
                              rot=(180, 0, 0), shape="RECTANGLE", size=lx * F, size_y=ly * F)
                    c.data.use_shadow = False
                    COVES.append(c)
            # LED cove: a thin glowing strip just under the ceiling along every side
            bm = bmesh.new()
            for a, b, c, e in ((x0, x1, y0, y0 + 0.08), (x0, x1, y1 - 0.08, y1), (x0, x0 + 0.08, y0, y1), (x1 - 0.08, x1, y0, y1)):
                bmesh.ops.create_cube(bm, size=1.0, matrix=Matrix.Translation(((a + b) / 2 * F, (c + e) / 2 * F, (H - 0.45) * F))
                                      @ Matrix.Diagonal(((b - a) * F, (e - c) * F, 0.05 * F)).to_4x4())
            me = bpy.data.meshes.new("cove")
            bm.to_mesh(me)
            ob = bpy.data.objects.new("cove", me)
            ob.data.materials.append(led)
            sc.collection.objects.link(ob)
# the coves have no shadows (cheap): keep them off the roof and parapet with light linking
roofs = bpy.data.collections.new("cove_excluded")
sc.collection.children.link(roofs)
for o in sc.objects:
    if o.type == "MESH" and o.name.startswith(("roof", "parapet")):
        roofs.objects.link(o)
try:
    for co in roofs.collection_objects:
        co.light_linking.link_state = "EXCLUDE"
    for c in COVES:
        c.light_linking.receiver_collection = roofs
except Exception as e:                       # older Blender: shadows instead
    print("light linking unavailable:", e)
    for c in COVES:
        c.data.use_shadow = True
# ------------------------------------------------------------------ restyle: warm neutrals like the reference
def tint(name, rgb, rough=None):
    m = bpy.data.materials.get(name)
    if not m or not m.use_nodes:
        return
    for n in m.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            bc = n.inputs["Base Color"]
            if bc.is_linked:                      # textured: multiply the texture by the tint
                mix = m.node_tree.nodes.new("ShaderNodeMix")
                mix.data_type, mix.blend_type = "RGBA", "MULTIPLY"
                mix.inputs[0].default_value = 1.0
                col_a, col_b = [i for i in mix.inputs if i.type == "RGBA"][:2]
                m.node_tree.links.new(bc.links[0].from_socket, col_a)
                col_b.default_value = (*rgb, 1)
                m.node_tree.links.new(next(o for o in mix.outputs if o.type == "RGBA"), bc)
            else:
                bc.default_value = (*rgb, 1)
            if rough is not None:
                n.inputs["Roughness"].default_value = rough
def recolour(name, rgb, rough=None):
    """replace a (textured) base colour by a flat one; bump / roughness maps stay"""
    m = bpy.data.materials.get(name)
    if not m:
        return
    for n in m.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            for l in list(n.inputs["Base Color"].links):
                m.node_tree.links.remove(l)
            n.inputs["Base Color"].default_value = (*rgb, 1)
            if rough is not None:
                n.inputs["Roughness"].default_value = rough


for name, rgb in (("cushion", (0.5, 0.45, 0.38)), ("sofa_fabric", (1.05, 1.02, 0.97)), ("rug_round", (0.62, 0.58, 0.52)),
                  ("rug", (0.55, 0.5, 0.44)), ("terrace", (0.82, 0.8, 0.77)), ("ceiling", (0.85, 0.83, 0.8))):
    recolour(name, rgb)
for name, rgb, rough in (("marble", (1.0, 0.9409, 0.8464), 0.12),       # (the tint squared: it used to be applied twice)
                         ("tile", (1.0, 0.96, 0.9), 0.2)):
    tint(name, rgb, rough)
for o in sc.objects:                              # flat white roof like a modern villa
    if o.type == "MESH" and o.name.startswith("roof") and bpy.data.materials.get("exterior"):
        o.data.materials.clear()
        o.data.materials.append(bpy.data.materials["exterior"])

# ------------------------------------------------------------------ renderer
eng = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
sc.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in eng else "BLENDER_EEVEE"
ee = sc.eevee
for k, v in (("shadow_pool_size", "1024"), ("taa_render_samples", opt("--samples", TOUR.get("samples", 32))), ("use_raytracing", True), ("use_shadows", True),
             ("shadow_ray_count", 2), ("shadow_step_count", 8), ("use_fast_gi", True), ("fast_gi_method", "GLOBAL_ILLUMINATION"),
             ("ray_tracing_method", "SCREEN"), ("use_volumetric_shadows", False), ("gi_diffuse_bounces", 3)):
    try:
        setattr(ee, k, v)
    except Exception as e:
        print("eevee setting skipped", k, e)
try:
    ee.ray_tracing_options.resolution_scale = "2"
    ee.ray_tracing_options.use_denoise = True
except Exception:
    pass
sc.render.resolution_x, sc.render.resolution_y = RES
sc.render.resolution_percentage = 100
sc.render.fps = FPS
sc.view_settings.view_transform = "AgX"
sc.view_settings.look = "AgX - Medium High Contrast"
sc.view_settings.exposure = TOUR.get("exposure", 0.9)
sc.render.image_settings.file_format = "JPEG"
sc.render.image_settings.quality = 94
cam = bpy.data.objects.new("tourcam", bpy.data.cameras.new("tourcam"))
sc.collection.objects.link(cam)
cam.data.lens, cam.data.sensor_width = TOUR.get("lens", 18), 36
cam.data.clip_start = 0.05
sc.camera = cam


def pose(t):
    p, q = at(t, "p") * F, at(t, "t") * F
    cam.location = p
    cam.rotation_euler = (q - p).to_track_quat("-Z", "Y").to_euler()


# ------------------------------------------------------------------ heat locks
GPU_HOT, GPU_OK = TOUR.get("gpu_hot", 80), TOUR.get("gpu_ok", 70)
CPU_HOT, CPU_OK = TOUR.get("cpu_hot", 92), TOUR.get("cpu_ok", 78)


def temps():
    g = c = 0
    try:
        g = int(subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"],
                               capture_output=True, text=True, timeout=5).stdout.split()[0])
    except Exception:
        pass
    reads = []                                  # package temp spikes for a moment at each frame start:
    for k in range(3):                          # lock on the lowest of 3 reads so only sustained heat counts
        for z in glob.glob("/sys/class/thermal/thermal_zone*"):
            try:
                if open(z + "/type").read().strip() == "x86_pkg_temp":
                    reads.append(int(open(z + "/temp").read()) // 1000)
            except Exception:
                pass
        time.sleep(0.25 * (k < 2))
    return g, min(reads, default=c)


def render_to(path):
    g, c = temps()
    if g >= GPU_HOT or c >= CPU_HOT:
        print(f"HEAT LOCK gpu {g}C cpu {c}C - cooling to gpu {GPU_OK}C / cpu {CPU_OK}C", flush=True)
        while g > GPU_OK or c > CPU_OK:
            time.sleep(5)
            g, c = temps()
        print(f"resume gpu {g}C cpu {c}C", flush=True)
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)
    return g, c


json.dump(dict(fps=FPS, duration=DUR, rooms=[(round(times[k], 2), keys[k]["room"]) for k in range(len(keys)) if keys[k].get("room")]),
          open(os.path.join(OUT, "timeline.json"), "w"), indent=1)
print(f"TOUR {DUR:.1f} s = {int(DUR * FPS)} frames at {FPS} fps, {RES[0]}x{RES[1]}, engine {sc.render.engine}", flush=True)

if "--stills" in argv:
    for t in [float(v) for v in argv[argv.index("--stills") + 1].split(",")]:
        pose(t)
        t0 = time.time()
        g, c = render_to(os.path.join(OUT, f"still_{t:06.1f}.jpg"))
        print(f"STILL {t:.1f}s {room_at(t)} {time.time() - t0:.1f}s gpu {g}C cpu {c}C", flush=True)

if "--shots" in argv:                           # stills from fixed cameras: [{"name", "p": [x,y,z], "t": [x,y,z], "lens"}] (feet)
    for sh in json.load(open(argv[argv.index("--shots") + 1])):
        cam.location = Vector(sh["p"]) * F
        cam.rotation_euler = ((Vector(sh["t"]) - Vector(sh["p"])) * F).to_track_quat("-Z", "Y").to_euler()
        cam.data.lens = sh.get("lens", 18)
        if "--save" in argv:                    # keep each view as a named camera in the saved scene
            c = cam.copy(); c.data = cam.data.copy(); c.name = c.data.name = "view_" + sh["name"]
            sc.collection.objects.link(c)
            continue
        t0 = time.time()
        g, c = render_to(os.path.join(OUT, sh["name"] + ".jpg"))
        print(f"SHOT {sh['name']} {time.time() - t0:.1f}s gpu {g}C cpu {c}C", flush=True)

if "--save" in argv:                            # self-contained .blend: lights, cameras, every texture packed inside
    bpy.ops.file.pack_all()
    bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(argv[argv.index("--save") + 1]), copy=True, compress=True)
    print("SAVED", flush=True)

if "--frames" in argv:
    fd = os.path.join(OUT, "frames")
    os.makedirs(fd, exist_ok=True)
    n = int(DUR * FPS) + 1
    for i in range(n):                          # bake the camera path into keyframes
        pose(i / FPS)
        cam.keyframe_insert("location", frame=i)
        cam.keyframe_insert("rotation_euler", frame=i)
    sc.frame_start, sc.frame_end, sc.frame_step = opt("--start", 0), n - 1, opt("--step", 1)   # step 2: odd frames blended later
    sc.render.filepath = os.path.join(fd, "f_#####")
    sc.render.use_overwrite, sc.render.use_placeholder = False, True      # resumable: done frames are skipped
    for f in glob.glob(os.path.join(fd, "f_*")):        # an empty placeholder is a frame cut off by a stop: render it again
        if os.path.getsize(f) == 0:
            os.remove(f)
    stamp = [time.time()]

    def heat_lock(scene, *_):
        g, c = temps()
        print(f"FRAME {scene.frame_current}/{n} {time.time() - stamp[0]:.1f}s gpu {g}C cpu {c}C", flush=True)
        if g >= GPU_HOT or c >= CPU_HOT:
            print(f"HEAT LOCK gpu {g}C cpu {c}C - cooling to gpu {GPU_OK}C / cpu {CPU_OK}C", flush=True)
            while g > GPU_OK or c > CPU_OK:
                time.sleep(5)
                g, c = temps()
            print(f"resume gpu {g}C cpu {c}C", flush=True)
        stamp[0] = time.time()
    bpy.app.handlers.render_pre.append(heat_lock)
    bpy.ops.render.render(animation=True)
    print("FRAMES OK", flush=True)
