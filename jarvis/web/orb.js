/* Animierter Orb im Iron-Man-HUD-Stil (Canvas 2D): rotierendes neuronales Netz mit Signalkaskaden im Inneren.
 * Zustände: offline, idle, listening, thinking, speaking, executing, confirm, error
 * Pegel (0..1) und optional ein Frequenzspektrum steuern die Reaktion auf Stimme/Audio. */
(function () {
  "use strict";

  const PALETTE = {
    offline:   { c: [90, 110, 130],  speed: 0.15, energy: 0.1 },
    idle:      { c: [63, 208, 255],  speed: 0.35, energy: 0.25 },
    listening: { c: [77, 255, 184],  speed: 0.7,  energy: 0.6 },
    thinking:  { c: [140, 150, 255], speed: 1.8,  energy: 0.55 },
    speaking:  { c: [63, 208, 255],  speed: 0.8,  energy: 0.8 },
    executing: { c: [255, 179, 71],  speed: 1.3,  energy: 0.7 },
    confirm:   { c: [255, 179, 71],  speed: 0.5,  energy: 0.5 },
    error:     { c: [255, 93, 108],  speed: 0.4,  energy: 0.4 },
  };

  const lerp = (a, b, t) => a + (b - a) * t;
  const TAU = Math.PI * 2;

  /* Neuronales Netz: rotierende 3D-Kugel aus Neuronen, Synapsen zu den nächsten Nachbarn. Signale laufen
   * über die Synapsen; kommt eines an, feuert das Neuron und löst mit einer zustandsabhängigen
   * Wahrscheinlichkeit Folgesignale aus (Kaskaden). */
  const NET = {
    // rate: spontane Zündungen/s · spread: Weitergabe-Wahrscheinlichkeit je Synapse · speed: Signaltempo
    // Bei ~3,5 weiterführenden Synapsen pro Neuron klingen Kaskaden unter spread ≈ 0,28 von selbst ab;
    // nur beim Denken ist das Netz bewusst „überkritisch“ und läuft voll.
    offline:   { rate: 0.15, spread: 0.1, speed: 0.6 },
    idle:      { rate: 1.4, spread: 0.2, speed: 0.9 },
    listening: { rate: 2, spread: 0.22, speed: 1.4, level: 40 },
    thinking:  { rate: 18, spread: 0.34, speed: 2.2 },
    speaking:  { rate: 2, spread: 0.24, speed: 1.7, level: 45 },
    executing: { rate: 10, spread: 0.27, speed: 1.9 },
    confirm:   { rate: 2, spread: 0.2, speed: 0.8 },
    error:     { rate: 1, spread: 0.15, speed: 0.7 },
  };
  const MAX_PULSES = 320;

  class NeuralNet {
    constructor(count) {
      this.nodes = [];
      const golden = Math.PI * (3 - Math.sqrt(5));
      for (let i = 0; i < count; i++) {
        // Fibonacci-Kugel, leicht zufällig nach innen versetzt → Volumen statt Hülle
        const y = 1 - (i / (count - 1)) * 2;
        const r = Math.sqrt(1 - y * y);
        const a = golden * i;
        const depth = 0.45 + 0.55 * Math.cbrt(Math.random());
        this.nodes.push({
          x: Math.cos(a) * r * depth + (Math.random() - 0.5) * 0.08,
          y: y * depth + (Math.random() - 0.5) * 0.08,
          z: Math.sin(a) * r * depth + (Math.random() - 0.5) * 0.08,
          fire: 0, links: [], px: 0, py: 0, pz: 0, ps: 1,
        });
      }
      // Synapsen: jedes Neuron zu seinen 3 nächsten Nachbarn
      const edges = new Set();
      this.nodes.forEach((n, i) => {
        const near = this.nodes
          .map((m, j) => [j, (m.x - n.x) ** 2 + (m.y - n.y) ** 2 + (m.z - n.z) ** 2])
          .filter(([j]) => j !== i)
          .sort((a, b) => a[1] - b[1])
          .slice(0, 3);
        for (const [j] of near) edges.add(i < j ? `${i}-${j}` : `${j}-${i}`);
      });
      this.edges = [...edges].map((e) => e.split("-").map(Number));
      for (const [a, b] of this.edges) {
        this.nodes[a].links.push(b);
        this.nodes[b].links.push(a);
      }
      this.pulses = [];
      this.acc = 0;
      this.rotY = 0;
    }

    fire(i, from, spread, speed) {
      const n = this.nodes[i];
      n.fire = 1;
      for (const j of n.links) {
        if (j === from || this.pulses.length >= MAX_PULSES) continue;
        if (Math.random() < spread) {
          this.pulses.push({ a: i, b: j, t: 0, v: speed * (0.7 + Math.random() * 0.6) });
        }
      }
    }

    update(dt, orb) {
      const cfg = NET[orb.state] || NET.idle;
      this.rotY += dt * (0.1 + orb.speed * 0.12);
      this.rotX = 0.35 * Math.sin(orb.t * 0.13);
      const rate = cfg.rate + (cfg.level ? orb.level * cfg.level : 0);
      this.acc += rate * dt;
      while (this.acc >= 1) {
        this.acc -= 1;
        this.fire((Math.random() * this.nodes.length) | 0, -1, cfg.spread + 0.12, cfg.speed);
      }
      const decay = Math.exp(-dt * 3.2);
      for (const n of this.nodes) n.fire *= decay;
      const alive = [];
      for (const p of this.pulses) {
        p.t += dt * p.v;
        if (p.t >= 1) this.fire(p.b, p.a, cfg.spread, cfg.speed);
        else alive.push(p);
      }
      this.pulses = alive.length > MAX_PULSES ? alive.slice(-MAX_PULSES) : alive;
    }

    draw(ctx, R, orb) {
      const cy = Math.cos(this.rotY), sy = Math.sin(this.rotY);
      const cx = Math.cos(this.rotX), sx = Math.sin(this.rotX);
      const f = 2.6;
      for (const n of this.nodes) {
        const x1 = n.x * cy - n.z * sy;
        const z1 = n.x * sy + n.z * cy;
        const y2 = n.y * cx - z1 * sx;
        const z2 = n.y * sx + z1 * cx;
        const s = f / (f - z2);
        n.px = x1 * R * s;
        n.py = y2 * R * s;
        n.pz = (z2 + 1) / 2;  // 0 = hinten, 1 = vorne
        n.ps = s;
      }
      // Synapsen
      ctx.lineWidth = 0.8;
      for (const [a, b] of this.edges) {
        const na = this.nodes[a], nb = this.nodes[b];
        const depth = (na.pz + nb.pz) / 2;
        const act = Math.max(na.fire, nb.fire);
        ctx.strokeStyle = orb._rgba(0.04 + depth * 0.14 + act * 0.35, act * 60);
        ctx.beginPath();
        ctx.moveTo(na.px, na.py);
        ctx.lineTo(nb.px, nb.py);
        ctx.stroke();
      }
      // Signale mit kurzem Schweif
      ctx.lineCap = "round";
      for (const p of this.pulses) {
        const na = this.nodes[p.a], nb = this.nodes[p.b];
        const t0 = Math.max(0, p.t - 0.22);
        const x = na.px + (nb.px - na.px) * p.t, y = na.py + (nb.py - na.py) * p.t;
        const xt = na.px + (nb.px - na.px) * t0, yt = na.py + (nb.py - na.py) * t0;
        const depth = na.pz + (nb.pz - na.pz) * p.t;
        ctx.strokeStyle = orb._rgba(0.25 + depth * 0.55, 90);
        ctx.lineWidth = 1 + depth * 1.4;
        ctx.beginPath();
        ctx.moveTo(xt, yt);
        ctx.lineTo(x, y);
        ctx.stroke();
        ctx.fillStyle = "rgba(255,255,255," + (0.35 + depth * 0.6).toFixed(3) + ")";
        ctx.beginPath();
        ctx.arc(x, y, 0.8 + depth * 1.3, 0, TAU);
        ctx.fill();
      }
      ctx.lineCap = "butt";
      // Neuronen (hinten zuerst)
      const order = this.nodes.slice().sort((a, b) => a.pz - b.pz);
      for (const n of order) {
        const r = (0.9 + n.pz * 1.6) * n.ps;
        if (n.fire > 0.05) {
          const g = ctx.createRadialGradient(n.px, n.py, 0, n.px, n.py, r * 7 * n.fire + r);
          g.addColorStop(0, orb._rgba(0.8 * n.fire, 120));
          g.addColorStop(1, "rgba(0,0,0,0)");
          ctx.fillStyle = g;
          ctx.beginPath();
          ctx.arc(n.px, n.py, r * 7 * n.fire + r, 0, TAU);
          ctx.fill();
        }
        ctx.fillStyle = orb._rgba(0.25 + n.pz * 0.6 + n.fire * 0.4, 40 + n.fire * 150);
        ctx.beginPath();
        ctx.arc(n.px, n.py, r + n.fire * 1.5, 0, TAU);
        ctx.fill();
      }
    }
  }

  class Orb {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.state = "offline";
      this.color = PALETTE.offline.c.slice();
      this.speed = PALETTE.offline.speed;
      this.energy = PALETTE.offline.energy;
      this.level = 0;
      this.targetLevel = 0;
      this.spectrum = null;
      this.t = 0;
      this.rot = 0;
      this.bars = new Float32Array(120);
      this.particles = Array.from({ length: 90 }, () => this._particle(true));
      this.net = new NeuralNet(150);
      this.reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      this._resize = this._resize.bind(this);
      window.addEventListener("resize", this._resize);
      this._resize();
      this.last = performance.now();
      requestAnimationFrame(this._frame.bind(this));
    }

    setState(s) { if (PALETTE[s]) this.state = s; }
    setLevel(v) { this.targetLevel = Math.max(0, Math.min(1, v)); }
    setSpectrum(arr) { this.spectrum = arr; }

    _particle(initial) {
      return {
        a: Math.random() * TAU,
        r: 1.05 + Math.random() * 0.75,
        v: (0.05 + Math.random() * 0.25) * (Math.random() < 0.5 ? -1 : 1),
        s: 0.6 + Math.random() * 1.6,
        life: initial ? Math.random() : 0,
      };
    }

    _resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const r = this.canvas.getBoundingClientRect();
      this.w = Math.max(1, r.width);
      this.h = Math.max(1, r.height);
      this.canvas.width = this.w * dpr;
      this.canvas.height = this.h * dpr;
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    _rgba(a, boost = 0) {
      const [r, g, b] = this.color;
      return `rgba(${Math.min(255, r + boost) | 0},${Math.min(255, g + boost) | 0},${Math.min(255, b + boost) | 0},${a})`;
    }

    _frame(now) {
      const dt = Math.min(0.05, (now - this.last) / 1000);
      this.last = now;
      const p = PALETTE[this.state];
      const k = 1 - Math.pow(0.02, dt);
      for (let i = 0; i < 3; i++) this.color[i] = lerp(this.color[i], p.c[i], k);
      this.speed = lerp(this.speed, p.speed, k);
      this.energy = lerp(this.energy, p.energy, k);
      this.level = lerp(this.level, this.targetLevel, 1 - Math.pow(0.0005, dt));
      const motion = this.reduced ? 0.25 : 1;
      this.t += dt * motion;
      this.rot += dt * this.speed * motion;
      this.net.update(dt * motion, this);
      this._draw();
      requestAnimationFrame(this._frame.bind(this));
    }

    _draw() {
      const { ctx, w, h } = this;
      ctx.clearRect(0, 0, w, h);
      const cx = w / 2, cy = h / 2 - h * 0.03;
      const R = Math.min(w, h) * 0.3;
      const lvl = this.level;
      const pulse = 1 + 0.04 * Math.sin(this.t * 2.2) * this.energy + lvl * 0.12;

      ctx.save();
      ctx.translate(cx, cy);
      ctx.globalCompositeOperation = "lighter";

      // Hintergrund-Glühen
      const glow = ctx.createRadialGradient(0, 0, R * 0.1, 0, 0, R * 2.1);
      glow.addColorStop(0, this._rgba(0.18 + lvl * 0.25));
      glow.addColorStop(0.45, this._rgba(0.05 + lvl * 0.05));
      glow.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = glow;
      ctx.beginPath(); ctx.arc(0, 0, R * 2.1, 0, TAU); ctx.fill();

      // Äußerer Skalenring mit Ticks
      ctx.save();
      ctx.rotate(this.rot * 0.15);
      for (let i = 0; i < 120; i++) {
        const a = (i / 120) * TAU;
        const long = i % 10 === 0;
        const r1 = R * 1.62, r2 = R * (long ? 1.72 : 1.67);
        ctx.strokeStyle = this._rgba(long ? 0.55 : 0.22);
        ctx.lineWidth = long ? 2 : 1;
        ctx.beginPath();
        ctx.moveTo(Math.cos(a) * r1, Math.sin(a) * r1);
        ctx.lineTo(Math.cos(a) * r2, Math.sin(a) * r2);
        ctx.stroke();
      }
      ctx.restore();

      // Segmentierte, gegenläufig rotierende Ringe
      this._segRing(R * 1.48, 3, 6, this.rot * 0.6, 0.55, 0.12);
      this._segRing(R * 1.36, 2, 24, -this.rot * 1.1, 0.35, 0.02);
      this._segRing(R * 1.24, 4, 3, this.rot * 1.7, 0.7, 0.35);

      // Scan-Kegel bei Ausführung/Denken
      if (this.state === "executing" || this.state === "thinking") {
        ctx.save();
        ctx.rotate(this.rot * 2.2);
        const sweep = ctx.createLinearGradient(0, 0, R * 1.5, 0);
        sweep.addColorStop(0, this._rgba(0.35));
        sweep.addColorStop(1, "rgba(0,0,0,0)");
        ctx.fillStyle = sweep;
        ctx.beginPath(); ctx.moveTo(0, 0); ctx.arc(0, 0, R * 1.5, -0.35, 0); ctx.closePath(); ctx.fill();
        ctx.restore();
      }

      // Audio-Balken
      const n = this.bars.length;
      const spec = this.spectrum;
      for (let i = 0; i < n; i++) {
        let target;
        const mirrored = i < n / 2 ? i : n - 1 - i;
        if (spec && spec.length) {
          const idx = Math.floor((mirrored / (n / 2)) * spec.length * 0.55) + 2;
          target = (spec[idx] || 0) / 255;
        } else {
          target = lvl * (0.55 + 0.45 * Math.sin(this.t * 9 + mirrored * 0.7));
          target += this.energy * 0.12 * (0.5 + 0.5 * Math.sin(this.t * 1.6 + mirrored * 0.35));
          if (this.state === "thinking") target += 0.2 * Math.max(0, Math.sin(this.t * 6 - i * 0.26));
        }
        this.bars[i] = lerp(this.bars[i], Math.max(0, target), 0.25);
      }
      ctx.save();
      ctx.rotate(-Math.PI / 2);
      for (let i = 0; i < n; i++) {
        const a = (i / n) * TAU;
        const len = R * (0.04 + this.bars[i] * 0.34);
        const r1 = R * 1.04;
        ctx.strokeStyle = this._rgba(0.35 + this.bars[i] * 0.6, 30);
        ctx.lineWidth = 2.2;
        ctx.beginPath();
        ctx.moveTo(Math.cos(a) * r1, Math.sin(a) * r1);
        ctx.lineTo(Math.cos(a) * (r1 + len), Math.sin(a) * (r1 + len));
        ctx.stroke();
      }
      ctx.restore();

      // Hauptring
      ctx.strokeStyle = this._rgba(0.9, 40);
      ctx.lineWidth = 2.5;
      ctx.shadowColor = this._rgba(1);
      ctx.shadowBlur = 18;
      ctx.beginPath(); ctx.arc(0, 0, R * pulse * 0.98, 0, TAU); ctx.stroke();
      ctx.shadowBlur = 0;

      // Weiches Leuchten im Zentrum, hinter dem Netz
      const coreR = R * (0.5 + lvl * 0.2) * pulse;
      const core = ctx.createRadialGradient(0, 0, 0, 0, 0, coreR);
      core.addColorStop(0, this._rgba(0.28 + this.energy * 0.2 + lvl * 0.25, 60));
      core.addColorStop(0.6, this._rgba(0.08));
      core.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = core;
      ctx.beginPath(); ctx.arc(0, 0, coreR, 0, TAU); ctx.fill();

      // Neuronales Netz
      this.net.draw(ctx, R * 0.86 * pulse, this);

      // Partikel
      for (const pt of this.particles) {
        pt.a += pt.v * 0.016 * (0.6 + this.speed);
        pt.life += 0.004 + this.energy * 0.004;
        if (pt.life > 1) Object.assign(pt, this._particle(false));
        const fade = Math.sin(pt.life * Math.PI);
        const r = R * (pt.r + lvl * 0.25 * Math.sin(this.t * 4 + pt.a * 3));
        ctx.fillStyle = this._rgba(0.55 * fade, 50);
        ctx.beginPath();
        ctx.arc(Math.cos(pt.a) * r, Math.sin(pt.a) * r, pt.s * (0.8 + lvl), 0, TAU);
        ctx.fill();
      }

      // Bestätigung: pulsierender Warnring
      if (this.state === "confirm" || this.state === "error") {
        const a = 0.35 + 0.35 * Math.sin(this.t * 5);
        ctx.strokeStyle = this._rgba(a);
        ctx.lineWidth = 3;
        ctx.setLineDash([14, 10]);
        ctx.beginPath(); ctx.arc(0, 0, R * 1.9, 0, TAU); ctx.stroke();
        ctx.setLineDash([]);
      }

      ctx.restore();
    }

    _segRing(radius, width, count, rot, alpha, gapRatio) {
      const ctx = this.ctx;
      const seg = TAU / count;
      ctx.save();
      ctx.rotate(rot);
      ctx.strokeStyle = this._rgba(alpha);
      ctx.lineWidth = width;
      for (let i = 0; i < count; i++) {
        const start = i * seg + seg * gapRatio;
        const end = (i + 1) * seg - seg * gapRatio;
        ctx.beginPath(); ctx.arc(0, 0, radius, start, end); ctx.stroke();
      }
      ctx.restore();
    }
  }

  window.Orb = Orb;
})();
