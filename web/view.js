// Walkthrough of a generated house: baked lighting (light maps) on unlit materials, so it runs on any GPU.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import * as walk from './walk.js';      // relative: the same file serves the downloaded website

const $ = (id) => document.getElementById(id);
const job = new URLSearchParams(location.search).get('job');
const site = !job;      // the downloaded website (no server): the house is in ./house/ and there is no /api
const base = site ? './house/' : `/jobs/${job}/`;
const WALK = 1.5, RUN = 3.0;

const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
renderer.setSize(innerWidth, innerHeight);
renderer.toneMapping = THREE.AgXToneMapping;
renderer.toneMappingExposure = 1.0;
document.body.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0xbfd6ea);
scene.fog = new THREE.Fog(0xbfd6ea, 60, 160);
const camera = new THREE.PerspectiveCamera(70, innerWidth / innerHeight, 0.05, 400);
const env = new THREE.PMREMGenerator(renderer).fromScene(new RoomEnvironment(), 0.04).texture;

const meta = await fetch(base + 'meta.json').then((r) => r.json());
const draco = new DRACOLoader().setDecoderPath((site ? '.' : '') + '/three/examples/jsm/libs/draco/');
const gltf = await new GLTFLoader().setDRACOLoader(draco).loadAsync(base + 'house.glb', (e) => {
  if (e.total) $('status').textContent = `Loading model… ${Math.round(100 * e.loaded / e.total)}%`;
});
const texLoader = new THREE.TextureLoader();
const lightmaps = {};
await Promise.all(Object.entries(meta.lightmaps || {}).map(async ([name, file]) => {
  const t = await texLoader.loadAsync(base + file);
  t.channel = 1;                       // second UV set (TEXCOORD_1)
  t.colorSpace = THREE.SRGBColorSpace;  // stored sRGB-encoded for precision in the shadows
  t.flipY = false;
  lightmaps[name] = t;
}));

// ---- materials: unlit * baked light; real-time light only for objects without a light map (trees)
const baked = !!meta.baked;
if (!baked) {
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8a7a66, 1.6));
  const sun = new THREE.DirectionalLight(0xfff1d6, 2.2);
  sun.position.set(-8, 14, 6);
  scene.add(sun);
} else {
  scene.add(new THREE.HemisphereLight(0xdfefff, 0x6d6450, 1.2));
  const sun = new THREE.DirectionalLight(0xfff1d6, 1.6);
  sun.position.set(-8, 14, 6);
  scene.add(sun);
}
const levels = [];   // level index -> objects (hidden above the current level in top view)
gltf.scene.traverse((o) => {
  if (!o.isMesh) return;
  let root = o;
  while (root.parent && root.parent !== gltf.scene) root = root.parent;
  const lm = lightmaps[root.name] || lightmaps[o.name];
  const lmax = (meta.lightmap_max || {})[root.name] ?? (meta.lightmap_max || {})[o.name] ?? 1;
  const conv = (m) => {
    const emissive = m.emissive && (m.emissive.r + m.emissive.g + m.emissive.b) > 0.01;
    const opts = { map: m.map, color: m.color, transparent: m.transparent, opacity: m.opacity, side: m.side,
                   alphaTest: m.alphaTest, depthWrite: !m.transparent };
    if (emissive) return new THREE.MeshBasicMaterial({ color: m.emissive.clone().multiplyScalar(Math.max(1, m.emissiveIntensity || 1)) });
    if (m.transparent) return new THREE.MeshBasicMaterial({ ...opts, envMap: env, reflectivity: 0.12, combine: THREE.MixOperation });
    // mirrors are pure metal: the baked (diffuse) light is black there, so show only the reflection
    if (m.metalness > 0.9 && m.roughness < 0.1) return new THREE.MeshBasicMaterial({ color: 0xe8ecef, envMap: env, reflectivity: 0.9, combine: THREE.MixOperation });
    if (lm) {
      const shiny = m.metalness > 0.5 ? 0.55 : m.roughness < 0.25 ? 0.12 : 0;
      return new THREE.MeshBasicMaterial({ ...opts, lightMap: lm, lightMapIntensity: Math.PI * lmax,
                                           envMap: shiny ? env : null, reflectivity: shiny, combine: THREE.MixOperation });
    }
    return new THREE.MeshLambertMaterial(opts);
  };
  o.material = Array.isArray(o.material) ? o.material.map(conv) : conv(o.material);
  const m = /^bake_level_(\d+)/.exec(root.name) || /_(\d+)$/.exec(root.name);
  if (m && /^(bake_level|glass)/.test(root.name)) (levels[+m[1]] ||= []).push(o);
});
scene.add(gltf.scene);

// ---- plan (x, y metres, z up) -> three (x, z, -y)
const W = (x, y, z) => new THREE.Vector3(x, z, -y);
const floors = meta.floors;
function roomAt(lv, x, y) {
  for (const r of floors[lv]?.rooms || []) for (const q of r.rects) if (walk.inRect(q, x, y)) return r.label;
  return '';
}

// ---- player
const p = { ...walk.entrance(meta), z: floors[0].z, yaw: 0, pitch: 0 };
const keys = {};
let locked = false, topView = false, measuring = false;
addEventListener('keydown', (e) => {
  if (tour) { if (!e.repeat) stopTour(); return; }      // any key ends the tour (a held G only repeats)
  keys[e.code] = true;
  if (e.code === 'KeyG' && !e.repeat) startTour();
  if (e.code === 'KeyT') toggleTop();
  if (e.code === 'KeyP' && !e.repeat) screenshot();
  if (e.code === 'KeyR' && !e.repeat) toggleRec();
  if (e.code === 'KeyM' && !e.repeat) toggleMap();
  if (e.code === 'KeyN' && !e.repeat) setMeasure(!measuring);
  if (e.code === 'Escape' && measuring) setMeasure(false);
});
addEventListener('keyup', (e) => { keys[e.code] = false; });
addEventListener('blur', () => { for (const k in keys) keys[k] = false; });      // a key let go in another window never sends keyup here
const canvas = renderer.domElement;
// back to walking: capture the mouse again, or looking around stays dead with nothing to click
const capture = () => { if (!matchMedia('(pointer: coarse)').matches) canvas.requestPointerLock()?.catch?.(() => { if (!topView && !measuring) $('overlay').style.display = 'flex'; }); };
$('overlay').addEventListener('click', () => {
  if (matchMedia('(pointer: coarse)').matches) { $('overlay').style.display = 'none'; return; }
  canvas.requestPointerLock();
});
document.addEventListener('pointerlockchange', () => {
  locked = document.pointerLockElement === canvas;
  $('overlay').style.display = locked || topView || measuring || tour ? 'none' : 'flex';
  if (locked && tour) document.exitPointerLock();      // (a lock asked for by leaving the top view lands late)
});
// the browser refused to capture the mouse: show the start screen, whose click captures it
document.addEventListener('pointerlockerror', () => { if (!topView && !measuring && !tour) $('overlay').style.display = 'flex'; });
addEventListener('mousemove', (e) => {
  if (tour || (!locked && !(measuring && !topView && e.buttons))) return;      // measuring frees the mouse: drag to look instead
  p.yaw -= e.movementX * 0.0022;
  p.pitch = Math.max(-1.4, Math.min(1.4, p.pitch - e.movementY * 0.0022));
});
// touch: left half = move stick, right half = look
const touch = { move: null, look: null, mx: 0, my: 0 };
canvas.addEventListener('touchstart', (e) => {
  for (const t of e.changedTouches) {
    if (t.clientX < innerWidth / 2 && !touch.move) touch.move = { id: t.identifier, x: t.clientX, y: t.clientY };
    else if (!touch.look) touch.look = { id: t.identifier, x: t.clientX, y: t.clientY };
  }
}, { passive: true });
canvas.addEventListener('touchmove', (e) => {
  if (topView) return;
  for (const t of e.changedTouches) {
    if (touch.move && t.identifier === touch.move.id) {
      touch.mx = Math.max(-1, Math.min(1, (t.clientX - touch.move.x) / 60));
      touch.my = Math.max(-1, Math.min(1, (t.clientY - touch.move.y) / 60));
    } else if (touch.look && t.identifier === touch.look.id) {
      p.yaw -= (t.clientX - touch.look.x) * 0.005;
      p.pitch = Math.max(-1.4, Math.min(1.4, p.pitch - (t.clientY - touch.look.y) * 0.005));
      touch.look.x = t.clientX; touch.look.y = t.clientY;
    }
  }
}, { passive: true });
canvas.addEventListener('touchend', (e) => {
  for (const t of e.changedTouches) {
    if (touch.move && t.identifier === touch.move.id) { touch.move = null; touch.mx = touch.my = 0; }
    if (touch.look && t.identifier === touch.look.id) touch.look = null;
  }
});

// ---- top view (dollhouse): storeys above the chosen one are hidden
const orbit = new OrbitControls(camera, canvas);
orbit.enabled = false;
let showLevel = 0;
function showLevels(n) {
  showLevel = n;
  levels.forEach((objs, i) => objs?.forEach((o) => { o.visible = !topView || i <= n; }));
  document.querySelectorAll('#floors button').forEach((b, i) => b.classList.toggle('on', topView && i === n));
}
function toggleTop() {
  topView = !topView;
  if (topView) {
    document.exitPointerLock?.();
    $('overlay').style.display = 'none';
    const b = meta.bounds, cx = (b[0] + b[1]) / 2, cy = (b[2] + b[3]) / 2, size = Math.max(b[1] - b[0], b[3] - b[2]);
    const z0 = floors[walk.levelOf(meta, p.z)].z;
    orbit.target.copy(W(cx, cy, z0));
    camera.position.copy(W(cx, cy - size * 0.75, z0 + size * 1.05));
    orbit.enabled = true;
    orbit.update();
    showLevels(walk.levelOf(meta, p.z));
  } else {
    orbit.enabled = false;
    showLevels(levels.length);
    if (!measuring) capture();
  }
  map.style.display = topView ? 'none' : 'block';
  $('cross').style.display = topView ? 'none' : 'block';
  $('topbtn').textContent = topView ? 'Walk' : 'Top view';
}
$('topbtn').onclick = toggleTop;
floors.forEach((f, i) => {
  const b = document.createElement('button');
  b.textContent = f.name;
  b.onclick = () => {
    if (topView) { showLevels(i); orbit.target.y = f.z; return; }
    // walk mode: jump to a free spot in the biggest room of that floor
    const r = (f.rooms.filter((q) => !['open', 'garden', 'yard', 'stair'].includes(q.type))
      .flatMap((q) => q.rects).sort((a, c) => (c[1] - c[0]) * (c[3] - c[2]) - (a[1] - a[0]) * (a[3] - a[2])))[0];
    if (r) { p.x = (r[0] + r[1]) / 2; p.y = (r[2] + r[3]) / 2; p.z = f.z; }
  };
  $('floors').appendChild(b);
});

// ---- tape measure (N): click or tap two points on the house. Line, ends and label are part of the 3D scene,
// so a screenshot or video taken while measuring shows the measurement too.
let tapeN = 0, downAt = null;
const ray = new THREE.Raycaster();
const ink = { color: 0xffd23f, depthTest: false, fog: false, toneMapped: false, transparent: true };   // seen through walls and furniture
const tapeGeo = new THREE.BufferGeometry().setAttribute('position', new THREE.Float32BufferAttribute(6, 3));
const tapeCv = Object.assign(document.createElement('canvas'), { width: 512, height: 96 });
const tapeTex = new THREE.CanvasTexture(tapeCv);
tapeTex.colorSpace = THREE.SRGBColorSpace;
const tapeLabel = new THREE.Sprite(new THREE.SpriteMaterial({ map: tapeTex, depthTest: false, fog: false, toneMapped: false, sizeAttenuation: false }));
tapeLabel.scale.set(0.24, 0.045, 1);      // a fixed size on screen, near or far
tapeLabel.center.set(0.5, -0.15);         // just above the line
const tape = new THREE.Group().add(new THREE.Line(tapeGeo, new THREE.LineBasicMaterial(ink)),
  new THREE.Points(tapeGeo, new THREE.PointsMaterial({ ...ink, size: 7, sizeAttenuation: false })), tapeLabel);
tape.children.forEach((o) => { o.renderOrder = 10; o.frustumCulled = false; });     // the points move: a cached bounding sphere would go stale
tape.visible = false;
scene.add(tape);
function setMeasure(on) {
  measuring = on;
  tapeN = 0;
  tape.visible = false;
  $('measure').classList.toggle('on', on);
  document.body.classList.toggle('measure', on);
  if (on) { document.exitPointerLock?.(); $('overlay').style.display = 'none'; }     // the mouse must be free to point
  else if (!topView) capture();
}
$('measure').onclick = () => setMeasure(!measuring);
canvas.addEventListener('pointerdown', (e) => { if (e.isPrimary) downAt = [e.clientX, e.clientY]; });
canvas.addEventListener('click', (e) => {
  // orbiting (top view) and looking (touch) end in a click too: only a click that did not drag measures
  if (!measuring || !downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 6) return;
  ray.setFromCamera(new THREE.Vector2(e.clientX / innerWidth * 2 - 1, 1 - e.clientY / innerHeight * 2), camera);
  const hit = ray.intersectObject(gltf.scene, true).find((h) => h.object.visible && !h.object.material.transparent);     // the ray also meets hidden storeys and window glass
  if (!hit) return;
  if (tapeN === 2) tapeN = 0;                      // a third click starts a new measurement
  const pos = tapeGeo.attributes.position;
  pos.setXYZ(tapeN++, hit.point.x, hit.point.y, hit.point.z);
  pos.needsUpdate = true;
  tapeGeo.setDrawRange(0, tapeN);
  tape.visible = true;
  tapeLabel.visible = tapeN === 2;
  if (tapeN < 2) return;
  const a = new THREE.Vector3().fromBufferAttribute(pos, 0), b = new THREE.Vector3().fromBufferAttribute(pos, 1);
  const d = a.distanceTo(b), inch = Math.round(d / 0.0254);
  tapeLabel.position.lerpVectors(a, b, 0.5);
  const g = tapeCv.getContext('2d'), text = `${d.toFixed(2)} m · ${Math.floor(inch / 12)}' ${inch % 12}"`;
  g.clearRect(0, 0, 512, 96);
  g.font = '600 52px system-ui, sans-serif';
  g.font = `600 ${Math.floor(52 * Math.min(1, 440 / g.measureText(text).width))}px system-ui, sans-serif`;     // long readings shrink to fit
  const tw = g.measureText(text).width + 40;
  g.fillStyle = 'rgba(14, 16, 20, .82)';
  g.beginPath(); g.roundRect(256 - tw / 2, 8, tw, 80, 40); g.fill();
  g.fillStyle = '#ffd23f'; g.textAlign = 'center'; g.textBaseline = 'middle';
  g.fillText(text, 256, 50);
  tapeTex.needsUpdate = true;
  window.__tape = text;                            // read by tests/test_site.mjs
});

// ---- map (M): the storey you are on from above, with room names, stairs, and where you stand and look
const map = $('map'), mg = map.getContext('2d');
const OUTSIDE = new Set(['open', 'garden', 'yard', 'balcony', 'parking']);
const roomName = (r) => (r.label || r.type).split(/\s+/).filter((w) => !/\d/.test(w)).join(' ').toUpperCase() || r.type.toUpperCase();   // without the size
let mapBig = false;
function toggleMap() { mapBig = !mapBig; map.classList.toggle('big', mapBig); document.body.classList.toggle('mapbig', mapBig); }
map.onclick = toggleMap;
function drawMap(lv) {
  const dpr = devicePixelRatio || 1, w = map.clientWidth, h = map.clientHeight;
  if (map.width !== Math.round(w * dpr) || map.height !== Math.round(h * dpr)) { map.width = Math.round(w * dpr); map.height = Math.round(h * dpr); }
  mg.setTransform(dpr, 0, 0, dpr, 0, 0);
  mg.clearRect(0, 0, w, h);
  const f = floors[lv], pad = mapBig ? 46 : 10, rs = [...f.colliders, ...f.rooms.flatMap((r) => r.rects)];
  const x0 = Math.min(...rs.map((r) => r[0])), x1 = Math.max(...rs.map((r) => r[1]));
  const y0 = Math.min(...rs.map((r) => r[2])), y1 = Math.max(...rs.map((r) => r[3]));
  const k = Math.min((w - 2 * pad) / (x1 - x0), (h - 2 * pad) / (y1 - y0));
  const X = (x) => (w - (x1 - x0) * k) / 2 + (x - x0) * k, Y = (y) => (h + (y1 - y0) * k) / 2 - (y - y0) * k;   // plan +y is up
  const box = (r) => mg.fillRect(X(r[0]), Y(r[3]), (r[1] - r[0]) * k, (r[3] - r[2]) * k);
  const here = f.rooms.find((r) => r.rects.some((q) => walk.inRect(q, p.x, p.y)));
  for (const r of f.rooms) {
    mg.fillStyle = r === here ? 'rgba(224, 122, 72, .5)' : OUTSIDE.has(r.type) ? 'rgba(120, 170, 110, .22)' : 'rgba(255, 255, 255, .13)';
    r.rects.forEach(box);
  }
  mg.fillStyle = 'rgba(196, 154, 108, .85)';
  (f.stairs || []).forEach((s) => box(s.rect));
  mg.fillStyle = 'rgba(255, 255, 255, .85)';
  f.colliders.forEach(box);
  const size = mapBig ? 13 : 9;
  mg.font = `600 ${size}px system-ui, sans-serif`;
  mg.textAlign = 'center';
  mg.textBaseline = 'middle';
  mg.fillStyle = '#fff';
  for (const r of f.rooms) {            // a name (and on the big map its size) is drawn only where it fits inside its room
    const q = r.rects.reduce((a, b) => ((b[1] - b[0]) * (b[3] - b[2]) > (a[1] - a[0]) * (a[3] - a[2]) ? b : a));
    const lines = [roomName(r)], area = r.rects.reduce((s, c) => s + (c[1] - c[0]) * (c[3] - c[2]), 0);
    if (mapBig) lines.push(`${(q[1] - q[0]).toFixed(2)} × ${(q[3] - q[2]).toFixed(2)} m`, `${area.toFixed(1)} m² · ${Math.round(area / 0.09290304)} sq ft`);
    const fits = (n) => size * (1.3 * n - 0.3) < (q[3] - q[2]) * k - 2 && lines.slice(0, n).every((t) => mg.measureText(t).width < (q[1] - q[0]) * k - 2);
    let n = lines.length;
    while (n && !fits(n)) n--;
    for (let i = 0; i < n; i++) mg.fillText(lines[i], X((q[0] + q[1]) / 2), Y((q[2] + q[3]) / 2) + (i - (n - 1) / 2) * size * 1.3);
  }
  // you: a view cone and a dot (yaw 0 looks along plan +y, turning left is positive)
  const px = X(p.x), py = Y(p.y), a = -Math.PI / 2 - p.yaw, len = mapBig ? 70 : 30;
  const cone = mg.createRadialGradient(px, py, 0, px, py, len);
  cone.addColorStop(0, 'rgba(255, 220, 190, .75)');
  cone.addColorStop(1, 'rgba(255, 220, 190, 0)');
  mg.fillStyle = cone;
  mg.beginPath(); mg.moveTo(px, py); mg.arc(px, py, len, a - 0.6, a + 0.6); mg.closePath(); mg.fill();
  mg.fillStyle = '#e07a48'; mg.strokeStyle = '#fff'; mg.lineWidth = 1.5;
  mg.beginPath(); mg.arc(px, py, mapBig ? 6 : 4, 0, 7); mg.fill(); mg.stroke();
  if (mapBig) {
    mg.fillStyle = '#fff';
    mg.textAlign = 'left';
    mg.font = '600 15px system-ui, sans-serif';
    mg.fillText([f.name, here && roomName(here)].filter(Boolean).join(' · '), 18, 22);
    mg.textAlign = 'right';
    mg.font = '13px system-ui, sans-serif';
    mg.fillStyle = 'rgba(255, 255, 255, .6)';
    mg.fillText('M to close', w - 18, 22);
  }
}

// ---- screenshot (P) and video recording (R): the 3D view only, without the buttons and labels
function save(blob, ext) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  const now = new Date(Date.now() - new Date().getTimezoneOffset() * 60000);      // local time in the file name
  a.download = `walkthrough-${now.toISOString().slice(0, 19).replace(/[T:]/g, '-')}.${ext}`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}
let rec = null;
function screenshot() {
  const pr = renderer.getPixelRatio();
  // about 4K worth of pixels, whatever the window size (not while recording: the video must keep one size)
  if (!rec) renderer.setPixelRatio(Math.max(pr, Math.sqrt(3840 * 2160 / (innerWidth * innerHeight))));
  renderer.render(scene, camera);          // the picture can only be read right after a render
  canvas.toBlob((b) => save(b, 'png'), 'image/png');
  renderer.setPixelRatio(pr);
}
const REC_TYPE = window.MediaRecorder && ['video/mp4;codecs=avc1.42E01E', 'video/webm;codecs=vp9', 'video/webm']
  .find((t) => MediaRecorder.isTypeSupported(t));
function toggleRec() {
  if (!REC_TYPE) return;
  if (rec) return rec.stop();
  const chunks = [], t0 = performance.now();
  const label = () => {
    const s = Math.floor((performance.now() - t0) / 1000);
    $('rec').textContent = `Stop ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  };
  rec = new MediaRecorder(canvas.captureStream(30), { mimeType: REC_TYPE, videoBitsPerSecond: 12e6 });
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  const tick = setInterval(label, 500);
  rec.onstop = () => {
    clearInterval(tick);
    rec = null;
    $('rec').textContent = 'Record';
    $('rec').classList.remove('live');
    const blob = new Blob(chunks, { type: REC_TYPE });
    if (REC_TYPE.includes('mp4')) return save(blob, 'mp4');
    if (site) return save(blob, 'webm');
    $('rec').textContent = 'Converting…';      // this browser records WebM: the server turns it into MP4
    fetch('/api/mp4', { method: 'POST', body: blob }).then((r) => (r.ok ? r.blob() : Promise.reject()))
      .then((mp4) => save(mp4, 'mp4'), () => save(blob, 'webm'))        // (no ffmpeg on the server: keep the WebM)
      .finally(() => { $('rec').textContent = 'Record'; });
  };
  rec.start(1000);
  $('rec').classList.add('live');
  label();
}
// realistic photos (Cycles), when the house was built with them: a gallery over the walkthrough
if (!site) fetch(`/api/jobs/${job}`).then((r) => r.json()).then((st) => {      // (the website has no /api and no photos)
  const names = (st.photos || []).filter((n) => /^[\w.-]+\.jpg$/.test(n));
  if (!names.length) return;
  $('photogrid').replaceChildren(...names.map((n) => {
    const fig = document.createElement('figure'), a = document.createElement('a'), im = new Image(), cap = document.createElement('figcaption');
    a.href = im.src = `${base}photos/${n}`;
    a.target = '_blank';
    im.alt = cap.textContent = n.slice(3, -4).replace(/_/g, ' ');
    im.loading = 'lazy';
    a.append(im);
    fig.append(a, cap);
    return fig;
  }));
  $('photosbtn').hidden = false;
  if (q.has('photos')) showPhotos();
});
function showPhotos() { document.exitPointerLock?.(); $('gallery').hidden = false; }
$('photosbtn').onclick = showPhotos;
$('closephotos').onclick = () => { $('gallery').hidden = true; };
addEventListener('keydown', (e) => { if (e.code === 'Escape') $('gallery').hidden = true; });
// ---- automatic tour (G): the route of walk.tourRoute, stepped like its test, recorded as one video
const LOOK = 5;           // seconds of looking around in each room
let tour = null, route = null;
function startTour() {
  if (measuring) setMeasure(false);      // the tape would be filmed
  if (topView) toggleTop();
  route ||= walk.tourRoute(meta);         // it depends on the house only
  if (route.length < 2) return;
  document.exitPointerLock?.();          // the mouse would fight the camera
  $('overlay').style.display = 'none';
  Object.assign(p, { x: route[0].x, y: route[0].y, z: route[0].z, pitch: 0 });
  tour = { route, i: 0, n: 0, budget: 0, look: -1, yaw: 0, cam: null, rec: !rec };
  if (tour.rec) toggleRec();
  $('tour').textContent = 'Stop tour';
  $('tour').classList.add('live');
}
function stopTour() {
  if (!tour) return;
  if (tour.rec && rec) toggleRec();      // stopping saves the video
  tour = null;
  $('tour').textContent = 'Tour';
  $('tour').classList.remove('live');
  $('overlay').style.display = locked || topView ? 'none' : 'flex';
}
function tourStep(dt) {
  const w = tour.route[tour.i];
  tour.cam = null;
  if (tour.look >= 0) {                  // pan left, right and back to the heading, like tools/tour.py films it
    const t = Math.min(1, tour.look += dt / LOOK);
    p.yaw = tour.yaw + 1.2 * Math.sin(2 * Math.PI * t * t * (3 - 2 * t));
    if (t === 1) { tour.look = -1; if (++tour.i === tour.route.length) stopTour(); }
    return;
  }
  const head = Math.atan2(p.x - w.x, w.y - p.y), turn = Math.atan2(Math.sin(head - p.yaw), Math.cos(head - p.yaw));   // shortest way round
  p.yaw += turn * Math.min(1, 3 * dt);   // eased turn towards the next waypoint
  p.pitch *= 1 - Math.min(1, 3 * dt);
  tour.budget += WALK * dt * Math.max(0, Math.cos(turn));     // slow down while turning; stand still to turn around
  for (; tour.budget >= walk.SUB; tour.budget -= walk.SUB) {
    if (Math.hypot(w.x - p.x, w.y - p.y) < 0.03) break;
    const bx = p.x, by = p.y;
    walk.stepTo(meta, p, w.x, w.y);
    // shortcut: a stuck (or endlessly sliding) walker hops to the waypoint so the recording cannot run forever;
    // tests/test_walk.mjs fails a route where that would happen
    if ((p.x === bx && p.y === by) || ++tour.n > 2000) Object.assign(p, { x: w.x, y: w.y, z: w.z });
  }
  const d = Math.hypot(w.x - p.x, w.y - p.y);
  if (d < 0.03) {                        // arrived: start the next leg exactly where the planner did
    Object.assign(p, { x: w.x, y: w.y, z: w.z });
    tour.budget = tour.n = 0;
    if (w.room) { tour.look = 0; tour.yaw = p.yaw; } else tour.i++;
    return;
  }
  // whole steps keep the walk exact; show the part of a step not yet taken so the camera glides
  const r = Math.min(tour.budget, d) / d;
  tour.cam = [p.x + (w.x - p.x) * r, p.y + (w.y - p.y) * r];
}
$('tour').onclick = startTour;
// a click or touch ends the tour, and does nothing else (a click on Record must not start a second video)
let eat = false;
addEventListener('pointerdown', () => { eat = !!tour; stopTour(); }, true);
addEventListener('click', (e) => { if (eat) { eat = false; e.stopPropagation(); } }, true);

$('blend').onclick = async () => {        // the server packs the textures into one .blend (a few seconds, once per build)
  const b = $('blend');
  b.disabled = true;
  b.textContent = 'Preparing…';
  try {
    const r = await fetch(`/api/jobs/${job}/export`, { method: 'POST' }), j = await r.json();
    if (!r.ok) throw new Error(j.error);
    const a = document.createElement('a');
    a.href = j.url;
    a.download = 'house-blender.zip';
    a.click();
  } catch (e) { alert(e.message || 'Could not prepare the Blender file.'); }
  b.disabled = false;
  b.textContent = 'Blender file';
};
$('site').onclick = async () => {         // the walkthrough as a static website, for clients who cannot reach this server
  const b = $('site');
  b.disabled = true;
  b.textContent = 'Preparing…';
  try {
    const r = await fetch(`/api/jobs/${job}/site`, { method: 'POST' }), j = await r.json();
    if (!r.ok) throw new Error(j.error);
    const a = document.createElement('a');
    a.href = j.url;
    a.download = 'house-website.zip';
    a.click();
  } catch (e) { alert(e.message || 'Could not prepare the website.'); }
  b.disabled = false;
  b.textContent = 'Website';
};
if (site) ['blend', 'site', 'newplan'].forEach((id) => $(id).remove());
$('shot').onclick = screenshot;
$('rec').onclick = toggleRec;
$('rec').hidden = !REC_TYPE;               // this browser cannot record video

// ---- loop
const clock = new THREE.Clock();
let frames = 0, fpsT = 0;
renderer.setAnimationLoop(() => {
  const raw = clock.getDelta(), dt = Math.min(raw, 0.05);
  frames++; fpsT += raw;
  if (fpsT > 0.5) { $('fps').textContent = `${Math.round(frames / fpsT)} fps`; frames = 0; fpsT = 0; }
  if (!topView && tour) tourStep(dt);
  if (!topView && !tour) {
    const sp = (keys.ShiftLeft || keys.ShiftRight ? RUN : WALK) * dt;
    let f = 0, s = 0;
    if (keys.KeyW || keys.ArrowUp) f += 1;
    if (keys.KeyS || keys.ArrowDown) f -= 1;
    if (keys.KeyD || keys.ArrowRight) s += 1;
    if (keys.KeyA || keys.ArrowLeft) s -= 1;
    f -= touch.my; s += touch.mx;
    if (keys.KeyQ) p.yaw += 1.8 * dt;
    if (keys.KeyE) p.yaw -= 1.8 * dt;
    const len = Math.hypot(f, s);
    if (len > 0.01) {
      const k = sp / Math.max(1, len);
      // plan axes: yaw 0 looks along +y (three -z)
      const dx = (-Math.sin(p.yaw) * f + Math.cos(p.yaw) * s) * k;
      const dy = (Math.cos(p.yaw) * f + Math.sin(p.yaw) * s) * k;
      walk.move(meta, p, dx, dy);
    }
  }
  if (!topView) {
    const [cx, cy] = tour?.cam || [p.x, p.y];
    camera.position.copy(W(cx, cy, p.z + meta.eye));
    camera.rotation.set(0, 0, 0);
    camera.rotation.order = 'YXZ';
    camera.rotation.y = p.yaw;
    camera.rotation.x = p.pitch;
    const lv = walk.levelOf(meta, p.z);
    $('room').textContent = [floors[lv].name, roomAt(lv, p.x, p.y)].filter(Boolean).join(' · ');
    drawMap(lv);
  } else orbit.update();
  renderer.render(scene, camera);
});
$('status').textContent = matchMedia('(pointer: coarse)').matches ? 'Tap to start' : 'Click to walk in';
// ?pose=x,y,level,yawDeg,pitchDeg (metres) and ?top=level : fixed views for previews / tests
const q = new URLSearchParams(location.search);
if (q.get('pose')) {
  const [x, y, lv, yaw, pitch] = q.get('pose').split(',').map(Number);
  Object.assign(p, { x, y, z: floors[lv || 0].z, yaw: (yaw || 0) * Math.PI / 180, pitch: (pitch || 0) * Math.PI / 180 });
  $('overlay').style.display = 'none';
}
if (q.get('top') !== null) { toggleTop(); showLevels(+q.get('top') || 0); }
if (q.get('embed') !== null) $('newplan')?.remove();        // inside another site's page there is no "new plan" to go to
// recording hook: draw one frame at a pose (metres, radians) and return it as a JPEG data URL
window.__frame = (x, y, z, yaw, pitch) => {
  renderer.setAnimationLoop(null);
  camera.position.copy(W(x, y, z + meta.eye));
  camera.rotation.order = 'YXZ';
  camera.rotation.set(pitch, yaw, 0);
  renderer.render(scene, camera);
  return renderer.domElement.toDataURL('image/jpeg', 0.92);
};
window.__ready = true;
addEventListener('resize', () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});
