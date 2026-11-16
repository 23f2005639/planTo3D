"""Realistic photos of a built house, rendered with Cycles (path tracing: real light, reflections, soft shadows).
blender -b JOB/house_baked.blend -P blender/photos.py -- JOB [--samples 128] [--res 1920x1080]
One JPEG per room in JOB/photos/, taken from the spot in the room with the clearest view of it.
Progress lines: 'PROGRESS <0-100> <message>'; 'PHOTO <file> <room label>' for each picture; 'PHOTOS OK' at the end."""
import json, math, os, re, sys
import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bake  # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:]
JOB = os.path.abspath(argv[0])
SAMPLES = int(argv[argv.index("--samples") + 1]) if "--samples" in argv else 128
RES = [int(v) for v in argv[argv.index("--res") + 1].split("x")] if "--res" in argv else [1920, 1080]
ROOMS = {"bedroom", "living", "dining", "kitchen", "toilet", "study", "puja", "room", "dress", "balcony"}
EYE = 1.5                                         # metres above the floor

meta = json.load(open(os.path.join(JOB, "meta.json")))
out = os.path.join(JOB, "photos")
os.makedirs(out, exist_ok=True)
for f in os.listdir(out):                         # a rebuild replaces the old set
    os.remove(os.path.join(out, f))

sc = bpy.context.scene
bake._gpu(sc)
sc.cycles.samples, sc.cycles.use_adaptive_sampling, sc.cycles.use_denoising = SAMPLES, True, True
sc.cycles.max_bounces, sc.cycles.diffuse_bounces = 8, 4
sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = *RES, 100
sc.render.image_settings.file_format, sc.render.image_settings.quality = "JPEG", 92
sc.view_settings.exposure = 0.6                   # rooms are lit for the light maps, which are normalised; photos are not
cam = bpy.data.objects.new("photo_cam", bpy.data.cameras.new("photo_cam"))
sc.collection.objects.link(cam)
sc.camera = cam
dg = bpy.context.evaluated_depsgraph_get()


def ray(o, d, far):
    hit, loc, *_ = sc.ray_cast(dg, o, d.normalized(), distance=far)
    return (loc - o).length if hit else far


def best_view(r, z, hero):
    """camera spot in the room rectangle r (x0, x1, y0, y1): corners and wall middles, inset 0.35 m, whose eye is not
    inside furniture. With a main piece of furniture (hero, its centre) the spot that sees it unblocked from about
    4 m wins and the camera looks between it and the room centre; otherwise the spot that sees farthest.
    -> (position, target) or None"""
    x0, x1, y0, y1 = r
    c = Vector(((x0 + x1) / 2, (y0 + y1) / 2, z + 1.0))
    t = c if hero is None else Vector((hero[0], hero[1], z + 0.8)).lerp(c, 0.35)
    i = 0.35
    spots = [(x, y) for x in (x0 + i, x1 - i) for y in (y0 + i, y1 - i)] + \
            [((x0 + x1) / 2, y0 + i), ((x0 + x1) / 2, y1 - i), (x0 + i, (y0 + y1) / 2), (x1 - i, (y0 + y1) / 2)]
    best, score = None, -1e9
    for x, y in spots:
        p = Vector((x, y, z + EYE))
        if ray(p, Vector((0, 0, -1)), EYE + 0.1) < EYE - 0.05:     # standing on a bed or a counter
            continue
        d = t - p
        a0 = math.atan2(d.y, d.x)
        s = sum(ray(p, Vector((math.cos(a0 + math.radians(a)), math.sin(a0 + math.radians(a)), -0.15)), 30)
                for a in (-40, -20, 0, 20, 40)) / 5
        if hero is not None:
            h = Vector((hero[0], hero[1], z + 0.8))
            seen = ray(p, h - p, 30) >= (h - p).length - 1.2       # nothing in the way but the item itself
            s = (10 if seen else 0) - abs((h - p).length - 4.0) + 0.1 * s
        if s > score:
            best, score = (p, t), s
    return best


def heroes():
    """centre (m) and floor area of the largest piece of furniture in each storey, from the same rules the build used"""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    import furnish
    plan = json.load(open(os.path.join(JOB, "plan.json")))
    out = []
    for f in plan["floors"]:
        items = [] if plan.get("furnish") is False else f.get("items") or furnish.furnish_floor(f)
        out.append([((b[0] + b[1]) / 2 * 0.3048, (b[2] + b[3]) / 2 * 0.3048, (b[1] - b[0]) * (b[3] - b[2]))
                    for b in (it.get("box") for it in items if it["fn"] not in SMALL) if b])
    return out


SMALL = {"rug", "round_rug", "wall_cab2", "chimney", "geyser", "model", "plant", "nightstand"}
HEROES = heroes()


photos = []
todo = [(f, rm) for f in meta["floors"] for rm in f["rooms"] if rm["type"] in ROOMS]
for k, (f, rm) in enumerate(todo):
    r = max(rm["rects"], key=lambda q: (q[1] - q[0]) * (q[3] - q[2]))
    if (r[1] - r[0]) * (r[3] - r[2]) < 2.5:       # m²: cupboards and ducts
        continue
    fi = meta["floors"].index(f)
    inside = [h for h in (HEROES[fi] if fi < len(HEROES) else []) if r[0] <= h[0] <= r[1] and r[2] <= h[1] <= r[3]]
    view = best_view(r, f["z"], max(inside, key=lambda h: h[2])[:2] if inside else None)
    if not view:
        continue
    print(f"PROGRESS {int(100 * k / len(todo))} photographing {rm['label']} ({k + 1} of {len(todo)})", flush=True)
    p, t = view
    cam.location = p
    cam.rotation_euler = (t - p).to_track_quat("-Z", "Y").to_euler()
    cam.data.lens = 14 if min(r[1] - r[0], r[3] - r[2]) < 2.5 else 18       # narrow rooms need a wider lens
    name = f"{len(photos) + 1:02d}_{re.sub(r'[^a-z0-9]+', '_', rm['label'].lower()).strip('_') or rm['type']}.jpg"
    sc.render.filepath = os.path.join(out, name)
    bpy.ops.render.render(write_still=True)
    photos.append(name)
    print(f"PHOTO {name} {f['name']} · {rm['label']}", flush=True)
print("PROGRESS 100 photos done", flush=True)
print("PHOTOS OK", flush=True)
