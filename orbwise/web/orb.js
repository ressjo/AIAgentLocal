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
  // Impuls-Wellen bei Zustandswechseln: Farbe, Anzahl Wellen, zündende Neuronen (aus der Mitte?)
  const IMPULSE = {
    listening: { c: [77, 255, 184], waves: 1, fire: 40, inner: true },
    speaking:  { c: null, waves: 1, fire: 25 },
    executing: { c: [255, 179, 71], waves: 1, fire: 18 },
    confirm:   { c: [255, 179, 71], waves: 1, fire: 12 },
    error:     { c: [255, 93, 108], waves: 2, fire: 25 },
  };
  const MAX_SATELLITES = 6;

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
        const nn = this.nodes[this.nodes.length - 1];
        nn.d = Math.sqrt(nn.x * nn.x + nn.y * nn.y + nn.z * nn.z);
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
      this.maxPulses = MAX_PULSES;
      this.acc = 0;
      this.rotY = 0;
    }

    fire(i, from, spread, speed) {
      const n = this.nodes[i];
      n.fire = 1;
      for (const j of n.links) {
        if (j === from || this.pulses.length >= this.maxPulses) continue;
        if (Math.random() < spread) {
          this.pulses.push({ a: i, b: j, t: 0, v: speed * (0.7 + Math.random() * 0.6) });
        }
      }
    }

    /* Viele Neuronen auf einmal zünden (Impuls, Startsequenz). inner = nur aus der Mitte heraus. */
    ignite(count, inner, spread = 0.3, speed = 1.6) {
      const pool = inner ? this.nodes.map((n, i) => [n.d, i]).sort((a, b) => a[0] - b[0]).slice(0, count * 2)
        .map((x) => x[1]) : null;
      for (let k = 0; k < count; k++) {
        const i = pool ? pool[(Math.random() * pool.length) | 0] : (Math.random() * this.nodes.length) | 0;
        this.fire(i, -1, spread, speed);
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
      this.pulses = alive.length > this.maxPulses ? alive.slice(-this.maxPulses) : alive;
    }

    draw(ctx, R, orb, q) {
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
      // Synapsen – gebündelt nach Helligkeit (ruhig / aktiv)
      const calm = q.lines(), hot = q.lines();
      for (const [a, b] of this.edges) {
        const na = this.nodes[a], nb = this.nodes[b];
        const act = Math.max(na.fire, nb.fire);
        const alpha = 0.04 + ((na.pz + nb.pz) / 2) * 0.14 + act * 0.35;
        (act > 0.15 ? hot : calm).add(alpha, na.px, na.py, nb.px, nb.py);
      }
      ctx.lineWidth = 0.8;
      calm.stroke(ctx, (al) => orb._rgba(al));
      hot.stroke(ctx, (al) => orb._rgba(al, 60));
      // Signale: Schweife gebündelt, Köpfe als ein Pfad
      const trails = q.lines();
      const heads = [];
      for (const p of this.pulses) {
        const na = this.nodes[p.a], nb = this.nodes[p.b];
        const t0 = Math.max(0, p.t - 0.22);
        const x = na.px + (nb.px - na.px) * p.t, y = na.py + (nb.py - na.py) * p.t;
        const depth = na.pz + (nb.pz - na.pz) * p.t;
        trails.add(0.25 + depth * 0.55, na.px + (nb.px - na.px) * t0, na.py + (nb.py - na.py) * t0, x, y);
        heads.push(x, y, 0.8 + depth * 1.3);
      }
      ctx.lineCap = "round";
      ctx.lineWidth = 1.6;
      trails.stroke(ctx, (al) => orb._rgba(al, 90));
      ctx.lineCap = "butt";
      if (heads.length) {
        ctx.fillStyle = "rgba(255,255,255,0.85)";
        ctx.beginPath();
        for (let i = 0; i < heads.length; i += 3) {
          ctx.moveTo(heads[i] + heads[i + 2], heads[i + 1]);
          ctx.arc(heads[i], heads[i + 1], heads[i + 2], 0, TAU);
        }
        ctx.fill();
      }
      // Leuchten feuernder Neuronen: vorgerendertes Sprite statt neuem Farbverlauf
      const sprite = orb._glowSprite();
      const base = ctx.globalAlpha;  // Startsequenz blendet das Netz ein
      for (const n of this.nodes) {
        if (n.fire > 0.08) {
          const r = (0.9 + n.pz * 1.6) * n.ps;
          const size = r * 7 * n.fire + r;
          ctx.globalAlpha = base * Math.min(1, 0.85 * n.fire);
          ctx.drawImage(sprite, n.px - size, n.py - size, size * 2, size * 2);
        }
      }
      ctx.globalAlpha = base;
      // Neuronen – gebündelt nach Helligkeit
      const dots = q.dots();
      for (const n of this.nodes) {
        const r = (0.9 + n.pz * 1.6) * n.ps + n.fire * 1.5;
        dots.add(0.25 + n.pz * 0.6 + n.fire * 0.4, n.px, n.py, r);
      }
      dots.fill(ctx, (al) => orb._rgba(al, 70));
    }
  }

  /* Bündelt viele Linien/Punkte nach Helligkeitsstufe → wenige Zeichenbefehle pro Frame. */
  const LEVELS = 8;
  class LineBatch {
    constructor() { this.b = Array.from({ length: LEVELS }, () => []); }
    add(alpha, x1, y1, x2, y2) {
      const l = Math.max(0, Math.min(LEVELS - 1, (alpha * LEVELS) | 0));
      this.b[l].push(x1, y1, x2, y2);
    }
    stroke(ctx, color) {
      for (let l = 0; l < LEVELS; l++) {
        const a = this.b[l];
        if (!a.length) continue;
        ctx.strokeStyle = color((l + 0.5) / LEVELS);
        ctx.beginPath();
        for (let i = 0; i < a.length; i += 4) { ctx.moveTo(a[i], a[i + 1]); ctx.lineTo(a[i + 2], a[i + 3]); }
        ctx.stroke();
        a.length = 0;
      }
    }
  }
  class DotBatch {
    constructor() { this.b = Array.from({ length: LEVELS }, () => []); }
    add(alpha, x, y, r) {
      const l = Math.max(0, Math.min(LEVELS - 1, (alpha * LEVELS) | 0));
      this.b[l].push(x, y, r);
    }
    fill(ctx, color) {
      for (let l = 0; l < LEVELS; l++) {
        const a = this.b[l];
        if (!a.length) continue;
        ctx.fillStyle = color((l + 0.5) / LEVELS);
        ctx.beginPath();
        for (let i = 0; i < a.length; i += 3) { ctx.moveTo(a[i] + a[i + 2], a[i + 1]); ctx.arc(a[i], a[i + 1], a[i + 2], 0, TAU); }
        ctx.fill();
        a.length = 0;
      }
    }
  }
  // Wiederverwendete Batches (keine Speicherallokation pro Frame)
  class BatchPool {
    constructor() { this.l = []; this.d = []; this.li = 0; this.di = 0; }
    reset() { this.li = 0; this.di = 0; }
    lines() { return this.l[this.li++] || (this.l[this.li - 1] = new LineBatch()); }
    dots() { return this.d[this.di++] || (this.d[this.di - 1] = new DotBatch()); }
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
      this.shocks = [];            // Impuls-Wellen
      this.lastPulse = 0;
      this.deform = 0;             // 0 = runder Hauptring, 1 = von der Stimme verformt
      this.satellites = new Map(); // Werkzeug-/Routinen-Anzeigen auf dem Skalenring
      this.labels = new Map();     // gerenderte Beschriftungen (Cache)
      this.bootT = null;           // Startsequenz: Sekunden seit Beginn, null = aus
      this.dt = 0.016;
      this.pool = new BatchPool();
      // Automatische Drosselung: bei dauerhaft niedriger Bildrate nur jedes 2. Frame zeichnen
      this.slow = false;
      this.frameCount = 0;
      this.periods = [];
      this.slowSince = 0;
      this.reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      this._resize = this._resize.bind(this);
      window.addEventListener("resize", this._resize);
      this._resize();
      this.last = performance.now();
      requestAnimationFrame(this._frame.bind(this));
    }

    setState(s) {
      if (!PALETTE[s] || s === this.state) return;
      this.state = s;
      if (IMPULSE[s]) this.pulse(s);
    }

    /* Spürbarer Moment: Welle(n) nach außen + das Netz zündet. */
    pulse(kind) {
      const now = performance.now();
      if (now - this.lastPulse < 400 || this.reduced || this.bootT !== null) return;
      this.lastPulse = now;
      const cfg = IMPULSE[kind] || IMPULSE.speaking;
      const c = (cfg.c || PALETTE[this.state].c).slice();
      for (let i = 0; i < cfg.waves; i++) {
        if (this.shocks.length >= 4) this.shocks.shift();
        this.shocks.push({ r: 1, delay: i * 0.18, c });
      }
      this.net.ignite(cfg.fire, !!cfg.inner);
    }

    /* Startsequenz: Ringe bauen sich nacheinander auf, dann zündet das Netz von innen nach außen. */
    boot() {
      if (this.reduced) return;
      this.bootT = 0;
      this.booted = { mid: false, all: false };
    }

    addSatellite(id, label) {
      if (this.satellites.has(id)) { this.satellites.get(id).leaving = false; return; }
      if (this.satellites.size >= MAX_SATELLITES) return;
      const n = this.satellites.size;
      this.satellites.set(id, { label: String(label).slice(0, 18), a: this.t * 0.25 - Math.PI / 2 + n * 0.6,
                                alpha: 0, leaving: false, err: false });
    }

    removeSatellite(id, failed = false) {
      const s = this.satellites.get(id);
      if (s) { s.leaving = true; s.err = s.err || failed; }
    }

    clearSatellites(keepPrefix = "") {
      for (const [id, s] of this.satellites) if (!keepPrefix || !id.startsWith(keepPrefix)) s.leaving = true;
    }

    _bootP(start, dur) {  // Fortschritt 0..1 eines Abschnitts der Startsequenz (1 = fertig bzw. keine Sequenz)
      if (this.bootT === null) return 1;
      return Math.max(0, Math.min(1, (this.bootT - start) / dur));
    }
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
      const dpr = Math.min(window.devicePixelRatio || 1, this.slow ? 1 : 1.5);
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

    _adapt(period) {
      this.periods.push(period);
      if (this.periods.length < 90) return;
      const avg = this.periods.reduce((a, b) => a + b, 0) / this.periods.length;
      this.periods.length = 0;
      const now = performance.now();
      if (!this.slow && avg > 24) {            // < ~40 fps → sparsamer
        this.slow = true;
        this.slowSince = now;
        this.net.maxPulses = 160;
        this._resize();
      } else if (this.slow && avg < 18 && now - this.slowSince > 20000) {  // wieder genug Luft
        this.slow = false;
        this.net.maxPulses = MAX_PULSES;
        this._resize();
      }
    }

    _frame(now) {
      requestAnimationFrame(this._frame.bind(this));
      const period = now - this.last;
      if (document.hidden || this.w < 2) { this.last = now; return; }
      this.frameCount++;
      if (this.slow && this.frameCount % 2) return;  // 30 fps
      this._adapt(this.slow ? period / 2 : period);
      const dt = Math.min(0.05, period / 1000);
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
      this.dt = dt * motion;
      this._tick(dt);
      this._draw();
    }

    _tick(dt) {
      // Wellen wachsen und verblassen
      for (const s of this.shocks) {
        if (s.delay > 0) s.delay -= dt;
        else s.r += dt * 1.5;
      }
      this.shocks = this.shocks.filter((s) => s.r < 2.3);
      // Ring-Verformung nur beim Sprechen/Zuhören
      const want = (this.state === "speaking" || this.state === "listening") && this.level > 0.02 ? 1 : 0;
      this.deform = lerp(this.deform, want, 1 - Math.pow(0.02, dt));
      // Satelliten: gleichmäßig verteilt, weich ein-/ausblenden
      const live = [...this.satellites.values()].filter((s) => !s.leaving);
      // im oberen Bogen (±115° um 12 Uhr) verteilt und leicht pendelnd – unten steht die Statuszeile
      live.forEach((s, i) => {
        const spread = live.length > 1 ? -2 + (4 * i) / (live.length - 1) : 0;
        const target = -Math.PI / 2 + spread * (live.length > 2 ? 1 : 0.6) + 0.18 * Math.sin(this.t * 0.35 + i);
        const d = ((target - s.a) % TAU + TAU * 1.5) % TAU - Math.PI;
        s.a += d * (1 - Math.pow(0.05, dt));
      });
      for (const [id, s] of this.satellites) {
        s.alpha = lerp(s.alpha, s.leaving ? 0 : 1, 1 - Math.pow(s.leaving ? 0.02 : 0.005, dt));
        if (s.leaving) s.a += dt * 0.6;
        if (s.leaving && s.alpha < 0.03) this.satellites.delete(id);
      }
      // Startsequenz
      if (this.bootT !== null) {
        this.bootT += dt;
        if (!this.booted.mid && this.bootT > 1.0) { this.booted.mid = true; this.net.ignite(30, true, 0.34, 1.4); }
        if (!this.booted.all && this.bootT > 1.35) {
          this.booted.all = true;
          this.shocks.push({ r: 1, delay: 0, c: PALETTE[this.state].c.slice() });
          this.net.ignite(30, false, 0.3, 1.8);
        }
        if (this.bootT > 1.9) this.bootT = null;
      }
    }

    _label(text) {
      let c = this.labels.get(text);
      if (!c) {
        c = document.createElement("canvas");
        const g = c.getContext("2d");
        const font = "600 10px ui-monospace, 'JetBrains Mono', monospace";
        g.font = font;
        const w = Math.ceil(g.measureText(text).width + text.length * 1.5) + 8;
        c.width = w * 2; c.height = 28;
        g.scale(2, 2);
        g.font = font;
        g.fillStyle = "rgba(225,245,255,0.95)";
        g.textBaseline = "middle";
        let x = 4;
        for (const ch of text) { g.fillText(ch, x, 7); x += g.measureText(ch).width + 1.5; }  // Sperrung
        c.w = w; c.h = 14;
        if (this.labels.size > 40) this.labels.clear();
        this.labels.set(text, c);
      }
      return c;
    }

    _glowSprite() {
      const key = this.color.map((c) => (c / 8) | 0).join(",");
      if (this._spriteKey !== key) {
        const c = this._sprite || (this._sprite = document.createElement("canvas"));
        c.width = c.height = 64;
        const g = c.getContext("2d");
        g.clearRect(0, 0, 64, 64);
        const grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
        grad.addColorStop(0, this._rgba(1, 150));
        grad.addColorStop(0.35, this._rgba(0.45, 60));
        grad.addColorStop(1, "rgba(0,0,0,0)");
        g.fillStyle = grad;
        g.fillRect(0, 0, 64, 64);
        this._spriteKey = key;
      }
      return this._sprite;
    }

    _draw() {
      const { ctx, w, h } = this;
      const q = this.pool;
      q.reset();
      ctx.clearRect(0, 0, w, h);
      const cx = w / 2, cy = h / 2 - h * 0.03;
      const R = Math.min(w, h) * 0.3;
      const lvl = this.level;
      const pulse = 1 + 0.04 * Math.sin(this.t * 2.2) * this.energy + lvl * 0.12;

      ctx.save();
      ctx.translate(cx, cy);
      ctx.globalCompositeOperation = "lighter";
      const bootScale = this._bootP(0, 0.6), bootSeg = this._bootP(0.35, 0.45), bootCore = this._bootP(0.75, 0.5);

      // Hintergrund-Glühen
      ctx.globalAlpha = bootCore;
      const glow = ctx.createRadialGradient(0, 0, R * 0.1, 0, 0, R * 2.1);
      glow.addColorStop(0, this._rgba(0.18 + lvl * 0.25));
      glow.addColorStop(0.45, this._rgba(0.05 + lvl * 0.05));
      glow.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = glow;
      ctx.fillRect(-R * 2.1, -R * 2.1, R * 4.2, R * 4.2);
      ctx.globalAlpha = 1;

      // Äußerer Skalenring: zwei Pfade (lange / kurze Striche)
      const rot = this.rot * 0.15;
      for (const long of [false, true]) {
        ctx.strokeStyle = this._rgba(long ? 0.55 : 0.22);
        ctx.lineWidth = long ? 2 : 1;
        ctx.beginPath();
        for (let i = long ? 0 : 1; i < 120 * bootScale; i += long ? 10 : 1) {
          if (!long && i % 10 === 0) continue;
          const a = (i / 120) * TAU + rot;
          const r1 = R * 1.62, r2 = R * (long ? 1.72 : 1.67);
          ctx.moveTo(Math.cos(a) * r1, Math.sin(a) * r1);
          ctx.lineTo(Math.cos(a) * r2, Math.sin(a) * r2);
        }
        ctx.stroke();
      }

      // Segmentierte, gegenläufig rotierende Ringe (je ein Pfad) – beim Start einrastend
      ctx.globalAlpha = bootSeg;
      const snap = (1 - bootSeg) * 1.2;
      this._segRing(R * 1.48, 3, 6, this.rot * 0.6 + snap, 0.55, 0.12);
      this._segRing(R * 1.36, 2, 24, -this.rot * 1.1 - snap, 0.35, 0.02);
      this._segRing(R * 1.24, 4, 3, this.rot * 1.7 + snap * 2, 0.7, 0.35);
      ctx.globalAlpha = bootCore;

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

      // Audio-Balken – gebündelt
      const n = this.bars.length;
      const spec = this.spectrum;
      const bars = q.lines();
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
        const a = (i / n) * TAU - Math.PI / 2;
        const len = R * (0.04 + this.bars[i] * 0.34);
        const r1 = R * 1.04;
        bars.add(0.35 + this.bars[i] * 0.6, Math.cos(a) * r1, Math.sin(a) * r1,
          Math.cos(a) * (r1 + len), Math.sin(a) * (r1 + len));
      }
      ctx.lineWidth = 2.2;
      bars.stroke(ctx, (al) => this._rgba(al, 30));

      // Hauptring: Leuchten über breite, transparente Linien statt teurem shadowBlur.
      // Beim Sprechen/Zuhören formt die Stimme den Ring (Radius folgt den geglätteten Audio-Balken).
      const ringR = R * pulse * 0.98 * (0.7 + 0.3 * bootCore);
      const ring = new Path2D();  // einmal bauen, dreimal zeichnen
      if (this.deform > 0.01) {
        const pts = this._ringShape(ringR, R);
        ring.moveTo(pts[0], pts[1]);
        for (let i = 2; i < pts.length; i += 2) ring.lineTo(pts[i], pts[i + 1]);
        ring.closePath();
      } else ring.arc(0, 0, ringR, 0, TAU);
      for (const [width, alpha, boost] of [[12, 0.07, 0], [6, 0.16, 20], [2.5, 0.9, 40]]) {
        ctx.strokeStyle = this._rgba(alpha, boost);
        ctx.lineWidth = width;
        ctx.stroke(ring);
      }

      // Impuls-Wellen
      for (const s of this.shocks) {
        if (s.delay > 0) continue;
        const k = 1 - (s.r - 1) / 1.3;
        const [r, g, b] = s.c;
        for (const [width, alpha] of [[10, 0.08], [2, 0.7]]) {
          ctx.strokeStyle = `rgba(${r | 0},${g | 0},${b | 0},${Math.max(0, alpha * k)})`;
          ctx.lineWidth = width * (0.6 + k * 0.4);
          ctx.beginPath(); ctx.arc(0, 0, R * s.r, 0, TAU); ctx.stroke();
        }
      }

      // Weiches Leuchten im Zentrum, hinter dem Netz
      const coreR = R * (0.5 + lvl * 0.2) * pulse;
      const core = ctx.createRadialGradient(0, 0, 0, 0, 0, coreR);
      core.addColorStop(0, this._rgba(0.28 + this.energy * 0.2 + lvl * 0.25, 60));
      core.addColorStop(0.6, this._rgba(0.08));
      core.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = core;
      ctx.fillRect(-coreR, -coreR, coreR * 2, coreR * 2);

      // Neuronales Netz
      this.net.draw(ctx, R * 0.86 * pulse, this, q);

      // Partikel – gebündelt
      const dots = q.dots();
      const flow = this.state === "listening" ? -1 : this.state === "speaking" ? 1 : 0;  // nach innen / außen
      for (const pt of this.particles) {
        pt.a += pt.v * 0.016 * (0.6 + this.speed);
        if (flow) {
          pt.r += flow * this.dt * (0.18 + lvl * 0.5);
          if (pt.r < 0.95) { pt.r = 1.8; pt.life = 0.05; }
          if (pt.r > 1.85) { pt.r = 1.05; pt.life = 0.05; }
        }
        pt.life += 0.004 + this.energy * 0.004;
        if (pt.life > 1) Object.assign(pt, this._particle(false));
        const fade = Math.sin(pt.life * Math.PI);
        const r = R * (pt.r + lvl * 0.25 * Math.sin(this.t * 4 + pt.a * 3));
        dots.add(0.55 * fade, Math.cos(pt.a) * r, Math.sin(pt.a) * r, pt.s * (0.8 + lvl));
      }
      dots.fill(ctx, (al) => this._rgba(al, 50));

      ctx.globalAlpha = 1;
      if (this.satellites.size) this._drawSatellites(R);

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

    _ringShape(ringR, R) {
      const N = 72, bars = this.bars, nb = bars.length;
      const pts = this._pts || (this._pts = new Float32Array(N * 2));
      const amp = R * 0.07 * this.deform;
      for (let k = 0; k < N; k++) {
        const a = (k / N) * TAU - Math.PI / 2;
        const v = bars[Math.floor((k / N) * nb)] || 0;
        const r = ringR + amp * (v - 0.15) * 1.4;
        pts[k * 2] = Math.cos(a) * r;
        pts[k * 2 + 1] = Math.sin(a) * r;
      }
      return pts;
    }

    _drawSatellites(R) {
      const ctx = this.ctx;
      const sprite = this._glowSprite();
      const rr = R * 1.36;  // auf dem feinen Segmentring – Beschriftung bleibt im Bild
      for (const s of this.satellites.values()) {
        const x = Math.cos(s.a) * rr, y = Math.sin(s.a) * rr;
        ctx.globalAlpha = s.alpha;
        const size = 14 + 3 * Math.sin(this.t * 6 + s.a * 3);
        ctx.drawImage(sprite, x - size, y - size, size * 2, size * 2);
        ctx.fillStyle = s.err ? "rgba(255,93,108,0.95)" : "rgba(255,255,255,0.9)";
        ctx.beginPath(); ctx.arc(x, y, 2.6, 0, TAU); ctx.fill();
        // Beschriftung zentriert über (obere Hälfte) bzw. unter (untere Hälfte) dem Punkt, nie auf dem Kopf
        const label = this._label(s.label);
        const ly = y + (Math.sin(s.a) < 0 ? -label.h - 5 : 5);
        ctx.drawImage(label, x - label.w / 2, ly, label.w, label.h);
      }
      ctx.globalAlpha = 1;
    }

    _segRing(radius, width, count, rot, alpha, gapRatio) {
      const ctx = this.ctx;
      const seg = TAU / count;
      ctx.strokeStyle = this._rgba(alpha);
      ctx.lineWidth = width;
      ctx.beginPath();
      for (let i = 0; i < count; i++) {
        const start = rot + i * seg + seg * gapRatio;
        const end = rot + (i + 1) * seg - seg * gapRatio;
        ctx.moveTo(Math.cos(start) * radius, Math.sin(start) * radius);
        ctx.arc(0, 0, radius, start, end);
      }
      ctx.stroke();
    }
  }

  window.Orb = Orb;
})();
