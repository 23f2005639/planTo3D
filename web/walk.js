// Walking rules shared by the viewer and its test: plan metres (x right, y forward, z up).
export const RADIUS = 0.22, STEP = 0.45;
// a single-storey plan's stairs lead to a floor that is not drawn: keep them closed. With 2+ storeys the
// top storey's stairs go up to the roof terrace and stay walkable.
const closed = (meta) => (meta.floors.length === 1 ? 0 : -1);
export const inRect = (r, x, y, m = 0) => x >= r[0] - m && x <= r[1] + m && y >= r[2] - m && y <= r[3] + m;

/** where a visit starts: the middle of the bottom-most room of the lowest storey (the entrance side of the plan).
 *  Voids and stairs do not count; of rooms equally low, the bigger one wins. Yaw 0 then looks up the plan, into the house. */
export function entrance(meta) {
  const area = (q) => (q[1] - q[0]) * (q[3] - q[2]);
  const rooms = meta.floors[0].rooms.filter((r) => !['open', 'stair', 'duct'].includes(r.type) && r.rects.length)
    .map((r) => ({ low: Math.min(...r.rects.map((q) => q[2])), big: r.rects.reduce((a, b) => (area(b) > area(a) ? b : a)) }));
  if (!rooms.length) { const b = meta.bounds; return { x: (b[0] + b[1]) / 2, y: (b[2] + b[3]) / 2 }; }
  const low = Math.min(...rooms.map((r) => r.low));
  const q = rooms.filter((r) => r.low < low + 0.3).sort((a, b) => area(b.big) - area(a.big))[0].big;
  return { x: (q[0] + q[1]) / 2, y: (q[2] + q[3]) / 2 };
}

export function levelOf(meta, z) {
  let lv = 0;
  meta.floors.forEach((f, i) => { if (z >= f.z - 0.3) lv = i; });
  return lv;
}

export function blockedAt(meta, lv, x, y) {
  const f = meta.floors[lv];
  if (!f) return false;
  for (const r of f.colliders) {
    const cx = Math.max(r[0], Math.min(x, r[1])), cy = Math.max(r[2], Math.min(y, r[3]));
    if ((x - cx) ** 2 + (y - cy) ** 2 < RADIUS * RADIUS) return true;
  }
  if (lv === closed(meta))
    for (const s of f.stairs || []) if (inRect(s.rect, x, y)) return true;   // (not where we come up)
  return false;
}

/** every walkable height under (x, y): storey floors, the roof, stair flights (a ramp) and landings */
export function heights(meta, x, y) {
  const planes = [...meta.floors.map((f) => f.z), meta.roof_z];
  const out = [];
  const shut = closed(meta);
  meta.floors.forEach((f, k) => {
    if (k === shut) return;
    for (const s of f.stairs || []) {
      if (!inRect(s.rect, x, y, 0.05)) continue;
      planes[k] = planes[k + 1] = null;      // steps cover this floor; the slab above has the stairwell hole
    }
  });
  out.push(...planes.filter((z) => z !== null));
  for (const f of meta.floors.filter((_, k) => k !== shut)) for (const s of f.stairs || []) {
    if (!inRect(s.rect, x, y, 0.05)) continue;
    if (s.landing) { out.push(s.z0); continue; }
    const a = s.axis === 'x' ? x : y;
    const len = s.axis === 'x' ? s.rect[1] - s.rect[0] : s.rect[3] - s.rect[2];
    const t = Math.min(1, Math.max(0, (a - s.start) * s.dir / len));
    out.push(s.z0 + (s.z1 - s.z0) * t);
  }
  return out;
}

/** the height closest to z that is within one step, or null (a drop / wall) */
export function groundNear(meta, x, y, z) {
  let best = null;
  for (const h of heights(meta, x, y)) if (Math.abs(h - z) <= STEP && (best === null || Math.abs(h - z) < Math.abs(best - z))) best = h;
  return best;
}

/** move the player p {x, y, z} by (dx, dy), sliding along walls; returns p */
export function move(meta, p, dx, dy) {
  for (const [ddx, ddy] of [[dx, 0], [0, dy]]) {        // one axis at a time: slide along walls
    if (!ddx && !ddy) continue;
    const nx = p.x + ddx, ny = p.y + ddy;
    const g = groundNear(meta, nx, ny, p.z);
    if (g === null) continue;
    if (blockedAt(meta, levelOf(meta, Math.max(g, p.z) - 0.05), nx, ny)) continue;
    p.x = nx; p.y = ny; p.z = g;
  }
  return p;
}

/** one tour step of at most SUB metres towards (tx, ty); the viewer and tests/test_walk.mjs step the same way, so a
 *  leg the planner checked is the leg the viewer walks */
export const SUB = 0.04;
export function stepTo(meta, p, tx, ty) {
  const dx = tx - p.x, dy = ty - p.y, d = Math.hypot(dx, dy), s = Math.min(SUB, d);
  if (d > 0) move(meta, p, dx / d * s, dy / d * s);
  return p;
}

// walk from a to b by SUB steps: arrives (within 3 cm, on b's height) or not
function reaches(meta, a, b) {
  const p = { x: a.x, y: a.y, z: a.z };
  for (let i = 0; i < 2000; i++) {
    if (Math.hypot(b.x - p.x, b.y - p.y) < 0.03) return Math.abs(p.z - b.z) < 0.05;
    const bx = p.x, by = p.y;
    stepTo(meta, p, b.x, b.y);
    if (p.x === bx && p.y === by) return false;
  }
  return false;
}

/** an automatic tour: from the entrance, room by room (the nearest next, the storey you are on first), through doors
 *  and up the stairs. -> waypoints [{x, y, z, room?, level?}]; a waypoint with a room is a stop to look around.
 *  Paths come from a breadth-first search on a 0.25 m grid that moves by walk.move's rules, then are straightened
 *  wherever stepping straight still arrives. The roof is left out: it is not a room. */
export function tourRoute(meta) {
  const C = 0.25, b = meta.bounds, x0 = b[0] - 1, y0 = b[2] - 1;
  const ni = Math.ceil((b[1] - b[0] + 2) / C), nj = Math.ceil((b[3] - b[2] + 2) / C);
  const nodes = new Map(), at = (i, j, z) => {      // a grid cell at one walkable height (floors, stair steps)
    const k = `${i},${j},${Math.round(z * 20)}`;
    if (!nodes.has(k)) nodes.set(k, { i, j, x: x0 + (i + 0.5) * C, y: y0 + (j + 0.5) * C, z, next: null });
    return nodes.get(k);
  };
  const next = (n) => n.next ||= [[1, 0], [-1, 0], [0, 1], [0, -1]].flatMap(([di, dj]) => {
    const i = n.i + di, j = n.j + dj;
    if (i < 0 || j < 0 || i >= ni || j >= nj) return [];
    const x = x0 + (i + 0.5) * C, y = y0 + (j + 0.5) * C, g = groundNear(meta, x, y, n.z);
    if (g === null || g > meta.roof_z - 0.1 || blockedAt(meta, levelOf(meta, Math.max(g, n.z) - 0.05), x, y)) return [];
    return [at(i, j, g)];
  });
  const bfs = (s) => {
    const from = new Map([[s, null]]), q = [s];
    for (let h = 0; h < q.length; h++) for (const m of next(q[h])) if (!from.has(m)) { from.set(m, q[h]); q.push(m); }
    return { from, order: q };
  };
  // start: the free cell nearest the entrance
  const e = entrance(meta), z0 = meta.floors[0].z;
  let start = null;
  for (let i = 0; i < ni; i++) for (let j = 0; j < nj; j++) {
    const x = x0 + (i + 0.5) * C, y = y0 + (j + 0.5) * C;
    if (blockedAt(meta, 0, x, y) || groundNear(meta, x, y, z0) !== z0) continue;
    if (!start || Math.hypot(x - e.x, y - e.y) < Math.hypot(start.x - e.x, start.y - e.y)) start = at(i, j, z0);
  }
  if (!start) return [];
  // a stop in each room worth a look: the reachable cell on its floor nearest the middle of its biggest part
  const reach = bfs(start).order, area = (q) => (q[1] - q[0]) * (q[3] - q[2]);
  const stops = [];
  meta.floors.forEach((f, lv) => {
    for (const r of f.rooms) {
      if (['open', 'duct', 'stair'].includes(r.type) || r.rects.reduce((a, q) => a + area(q), 0) < 2) continue;
      const q = r.rects.reduce((a, c) => (area(c) > area(a) ? c : a)), cx = (q[0] + q[1]) / 2, cy = (q[2] + q[3]) / 2;
      let best = null;
      for (const n of reach) {
        if (Math.abs(n.z - f.z) > 0.01 || !r.rects.some((c) => inRect(c, n.x, n.y))) continue;
        if (!best || Math.hypot(n.x - cx, n.y - cy) < Math.hypot(best.x - cx, best.y - cy)) best = n;
      }
      if (best) stops.push({ node: best, room: r.label, level: lv });
    }
  });
  const route = [{ x: start.x, y: start.y, z: start.z }];
  let here = start, lv = 0;
  while (stops.length) {
    const { from, order } = bfs(here), rank = new Map(order.map((n, k) => [n, k]));
    // nearest by walking distance (BFS order), finishing the storey you are on before taking the stairs
    const key = (s) => (s.level === lv ? 0 : 1e9) + (rank.get(s.node) ?? 2e9);
    const s = stops.reduce((a, c) => (key(c) < key(a) ? c : a));
    stops.splice(stops.indexOf(s), 1);
    if (!from.has(s.node)) continue;               // reached from the start but not from here (one-way step): skip it
    const path = [];
    for (let n = s.node; n; n = from.get(n)) path.unshift(n);
    for (let a = 0; a < path.length - 1;) {       // straighten: go as far ahead as a straight walk still arrives
      let k = a + 1;
      while (k + 1 < path.length && reaches(meta, path[a], path[k + 1])) k++;
      if (k < path.length - 1) route.push({ x: path[k].x, y: path[k].y, z: path[k].z });
      a = k;
    }
    route.push({ x: s.node.x, y: s.node.y, z: s.node.z, room: s.room, level: s.level });
    here = s.node; lv = s.level;
  }
  return route;
}
