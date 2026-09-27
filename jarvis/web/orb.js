/* Animierter "Arc-Reactor"-Orb im Iron-Man-HUD-Stil (Canvas 2D).
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

      // Arc-Reactor-Kern: dreieckige Speichen + Kernglühen
      ctx.save();
      ctx.rotate(-this.rot * 0.4);
      for (let i = 0; i < 10; i++) {
        const a0 = (i / 10) * TAU + 0.06, a1 = ((i + 1) / 10) * TAU - 0.06;
        ctx.fillStyle = this._rgba(0.1 + 0.08 * Math.sin(this.t * 3 + i));
        ctx.strokeStyle = this._rgba(0.45);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(0, 0, R * 0.82 * pulse, a0, a1);
        ctx.arc(0, 0, R * 0.52 * pulse, a1, a0, true);
        ctx.closePath(); ctx.fill(); ctx.stroke();
      }
      ctx.restore();

      ctx.save();
      ctx.rotate(this.rot * 0.9);
      ctx.strokeStyle = this._rgba(0.7, 60);
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      for (let i = 0; i <= 3; i++) {
        const a = (i / 3) * TAU - Math.PI / 2;
        const r = R * 0.42 * pulse;
        i === 0 ? ctx.moveTo(Math.cos(a) * r, Math.sin(a) * r) : ctx.lineTo(Math.cos(a) * r, Math.sin(a) * r);
      }
      ctx.stroke();
      ctx.restore();

      const coreR = R * (0.34 + lvl * 0.12) * pulse;
      const core = ctx.createRadialGradient(0, 0, 0, 0, 0, coreR);
      core.addColorStop(0, "rgba(255,255,255,0.95)");
      core.addColorStop(0.25, this._rgba(0.9, 80));
      core.addColorStop(0.7, this._rgba(0.35));
      core.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = core;
      ctx.beginPath(); ctx.arc(0, 0, coreR, 0, TAU); ctx.fill();

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
