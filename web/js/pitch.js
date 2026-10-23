// The 3D pitch: floodlit turf, chalk lines, 22 players, the ball, and three cameras.
import * as THREE from "three";

const L = 105, W = 68;
const toV = (x, y, z = 0) => new THREE.Vector3(x - L / 2, z, y - W / 2);

function pitchTexture() {
  const ppm = 16, c = document.createElement("canvas");
  c.width = L * ppm; c.height = W * ppm;
  const g = c.getContext("2d");
  const stripes = 18;
  for (let i = 0; i < stripes; i++) {
    g.fillStyle = i % 2 ? "#1a4a2f" : "#1d5434";
    g.fillRect((i * c.width) / stripes, 0, c.width / stripes + 1, c.height);
  }
  g.strokeStyle = "rgba(245,245,235,0.92)";
  g.lineWidth = 0.12 * ppm;
  const P = (x) => x * ppm;
  g.strokeRect(P(0.06), P(0.06), P(L - 0.12), P(W - 0.12));
  g.beginPath(); g.moveTo(P(L / 2), 0); g.lineTo(P(L / 2), P(W)); g.stroke();
  g.beginPath(); g.arc(P(L / 2), P(W / 2), P(9.15), 0, Math.PI * 2); g.stroke();
  g.fillStyle = "rgba(245,245,235,0.92)";
  for (const x of [L / 2, 11, L - 11]) { g.beginPath(); g.arc(P(x), P(W / 2), P(0.22), 0, Math.PI * 2); g.fill(); }
  for (const side of [0, 1]) {
    const x0 = side ? L - 16.5 : 0, x6 = side ? L - 5.5 : 0;
    g.strokeRect(P(x0), P(W / 2 - 20.16), P(16.5), P(40.32));
    g.strokeRect(P(x6), P(W / 2 - 9.16), P(5.5), P(18.32));
    g.beginPath();
    const cx = side ? L - 11 : 11, a = Math.acos(5.5 / 9.15);
    if (side) g.arc(P(cx), P(W / 2), P(9.15), Math.PI - a, Math.PI + a);
    else g.arc(P(cx), P(W / 2), P(9.15), -a, a);
    g.stroke();
  }
  const t = new THREE.CanvasTexture(c);
  t.anisotropy = 8;
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

export class Pitch {
  constructor(canvas, match) {
    this.match = match;
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color("#070b09");
    this.scene.fog = new THREE.Fog("#070b09", 140, 260);
    this.camera = new THREE.PerspectiveCamera(32, 16 / 9, 0.5, 600);
    this.mode = "broadcast";
    this.look = new THREE.Vector3();
    this.camPos = new THREE.Vector3(0, 30, 70);

    this.scene.add(new THREE.HemisphereLight("#dfe9ff", "#0c1a10", 0.9));
    const sun = new THREE.DirectionalLight("#ffffff", 1.6);
    sun.position.set(-30, 80, 40);
    this.scene.add(sun);

    const ground = new THREE.Mesh(new THREE.PlaneGeometry(L + 16, W + 16), new THREE.MeshLambertMaterial({ color: "#123a24" }));
    ground.rotation.x = -Math.PI / 2; ground.position.y = -0.02;
    this.scene.add(ground);
    const turf = new THREE.Mesh(new THREE.PlaneGeometry(L, W), new THREE.MeshLambertMaterial({ map: pitchTexture() }));
    turf.rotation.x = -Math.PI / 2;
    this.scene.add(turf);
    this.goals();
    this.stands();

    const body = new THREE.CapsuleGeometry(0.34, 1.15, 4, 10);
    const head = new THREE.SphereGeometry(0.2, 12, 10);
    const shadow = new THREE.CircleGeometry(0.55, 20);
    const shadowMat = new THREE.MeshBasicMaterial({ color: "#000", transparent: true, opacity: 0.35, depthWrite: false });
    this.players = [];
    for (let s = 0; s < 22; s++) {
      const side = s < 11 ? "home" : "away";
      const kit = match.meta[side].kit;
      const isGk = s === 0 || s === 11;
      const g = new THREE.Group();
      const mat = new THREE.MeshLambertMaterial({ color: isGk ? (side === "home" ? "#d9c632" : "#3fbf9a") : kit.primary });
      const b = new THREE.Mesh(body, mat); b.position.y = 0.92;
      const h = new THREE.Mesh(head, new THREE.MeshLambertMaterial({ color: "#c79b7a" })); h.position.y = 1.78;
      const sh = new THREE.Mesh(shadow, shadowMat); sh.rotation.x = -Math.PI / 2; sh.position.y = 0.01;
      g.add(b, h, sh);
      this.scene.add(g);
      this.players.push(g);
    }
    this.ring = new THREE.Mesh(new THREE.RingGeometry(0.75, 0.95, 32), new THREE.MeshBasicMaterial({ color: "#f2b84b", transparent: true, opacity: 0.9 }));
    this.ring.rotation.x = -Math.PI / 2; this.ring.position.y = 0.02; this.ring.visible = false;
    this.scene.add(this.ring);
    this.ball = new THREE.Mesh(new THREE.SphereGeometry(0.22, 16, 12), new THREE.MeshLambertMaterial({ color: "#ffffff", emissive: "#333" }));
    this.ballShadow = new THREE.Mesh(shadow, shadowMat); this.ballShadow.rotation.x = -Math.PI / 2; this.ballShadow.scale.setScalar(0.45);
    this.scene.add(this.ball, this.ballShadow);
    this.pos = new Float32Array(48);
    this.prev = null;
    this.heading = new Array(22).fill(0);
  }

  goals() {
    const mat = new THREE.MeshLambertMaterial({ color: "#f5f5f0" });
    const net = new THREE.MeshBasicMaterial({ color: "#ffffff", transparent: true, opacity: 0.12, side: THREE.DoubleSide });
    for (const x of [0, L]) {
      const dir = x === 0 ? -1 : 1, g = new THREE.Group();
      const post = new THREE.CylinderGeometry(0.06, 0.06, 2.44, 8);
      for (const y of [-3.66, 3.66]) { const p = new THREE.Mesh(post, mat); p.position.set(0, 1.22, y); g.add(p); }
      const bar = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, 7.32, 8), mat);
      bar.rotation.x = Math.PI / 2; bar.position.set(0, 2.44, 0); g.add(bar);
      const back = new THREE.Mesh(new THREE.PlaneGeometry(7.32, 2.44), net);
      back.position.set(dir * 1.8, 1.22, 0); back.rotation.y = Math.PI / 2; g.add(back);
      g.position.copy(toV(x, W / 2));
      this.scene.add(g);
    }
  }

  stands() {
    // Dark tiers and four floodlight banks: enough architecture to read as a stadium at night.
    const tier = new THREE.MeshLambertMaterial({ color: "#0d1411" });
    for (const [w, d, x, z, ry] of [[L + 30, 14, 0, -W / 2 - 16, 0], [L + 30, 14, 0, W / 2 + 16, 0], [W + 30, 14, -L / 2 - 16, 0, Math.PI / 2], [W + 30, 14, L / 2 + 16, 0, Math.PI / 2]]) {
      const m = new THREE.Mesh(new THREE.BoxGeometry(w, 10, d), tier);
      m.position.set(x, 4, z); m.rotation.y = ry; this.scene.add(m);
    }
    const glow = new THREE.MeshBasicMaterial({ color: "#eef4ff" });
    for (const [x, z] of [[-L / 2 - 14, -W / 2 - 14], [L / 2 + 14, -W / 2 - 14], [-L / 2 - 14, W / 2 + 14], [L / 2 + 14, W / 2 + 14]]) {
      const bank = new THREE.Mesh(new THREE.BoxGeometry(6, 2.2, 0.6), glow);
      bank.position.set(x, 34, z); bank.lookAt(0, 0, 0); this.scene.add(bank);
      const light = new THREE.PointLight("#eef4ff", 300, 160, 1.6);
      light.position.set(x, 32, z); this.scene.add(light);
    }
  }

  resize() {
    const c = this.renderer.domElement, w = c.clientWidth, h = c.clientHeight;
    if (c.width !== Math.floor(w * this.renderer.getPixelRatio()) || c.height !== Math.floor(h * this.renderer.getPixelRatio())) {
      this.renderer.setSize(w, h, false);
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
    }
  }

  // Place everything for a match time; returns the ball's pitch position for the graphics layer.
  update(ms, focusSlot, dt) {
    const p = this.match.sample(ms, this.pos);
    if (!p) return null;
    for (let s = 0; s < 22; s++) {
      const x = p[s * 2], y = p[s * 2 + 1], g = this.players[s];
      const off = y < -1.5 || y > W + 1.5;
      g.visible = !off;
      if (this.prev) {
        const dx = x - this.prev[s * 2], dy = y - this.prev[s * 2 + 1];
        if (dx * dx + dy * dy > 0.0004) this.heading[s] = Math.atan2(dx, dy);
      }
      g.position.copy(toV(x, y));
      g.rotation.y = this.heading[s];
    }
    this.prev = Float32Array.from(p);
    const bx = p[44], by = p[45], bz = Math.max(0.11, p[46]);
    this.ball.position.copy(toV(bx, by, bz + 0.11));
    this.ballShadow.position.copy(toV(bx, by, 0.015));
    this.ring.visible = focusSlot != null && this.players[focusSlot]?.visible;
    if (this.ring.visible) this.ring.position.set(this.players[focusSlot].position.x, 0.02, this.players[focusSlot].position.z);
    this.aim(bx, by, focusSlot, dt);
    return { x: bx, y: by, z: bz, positions: p };
  }

  aim(bx, by, focusSlot, dt) {
    const k = 1 - Math.exp(-dt * 2.2);
    const ball = toV(bx, by);
    let pos, look;
    if (this.mode === "tactical") {
      pos = new THREE.Vector3(ball.x * 0.2, 128, 40);
      look = new THREE.Vector3(ball.x * 0.2, 0, 2);
    } else if (this.mode === "player" && focusSlot != null && this.players[focusSlot].visible) {
      const pl = this.players[focusSlot].position;
      const side = focusSlot < 11 ? "home" : "away";
      const t = this.match.meta;
      const attacksPositive = (side === "home") === (this.period === 1);
      const dir = attacksPositive ? 1 : -1;
      pos = new THREE.Vector3(pl.x - dir * 14, 9, pl.z + 6);
      look = new THREE.Vector3(pl.x + dir * 12, 0, pl.z * 0.7);
    } else {
      pos = new THREE.Vector3(ball.x * 0.8, 31, W / 2 + 52);
      look = new THREE.Vector3(ball.x * 0.9, 0, ball.z * 0.35);
    }
    this.camPos.lerp(pos, k);
    this.look.lerp(look, k);
    this.camera.position.copy(this.camPos);
    this.camera.lookAt(this.look);
  }

  render() { this.renderer.render(this.scene, this.camera); }

  // Screen position for a pitch point at a height, in CSS pixels of the canvas.
  project(x, y, z, out = {}) {
    const v = toV(x, y, z).project(this.camera);
    const c = this.renderer.domElement;
    out.x = (v.x * 0.5 + 0.5) * c.clientWidth;
    out.y = (-v.y * 0.5 + 0.5) * c.clientHeight;
    out.visible = v.z < 1 && v.x > -1.1 && v.x < 1.1 && v.y > -1.1 && v.y < 1.1;
    return out;
  }
}
