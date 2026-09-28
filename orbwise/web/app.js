/* Orbwise – Web-Client: WebSocket, Chat, Tool-Aktivität, Bestätigungen, Mikrofon und Sprachausgabe. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const orb = new window.Orb($("orb"));
  // Sprache: der Server liefert index.html bereits mit lang="de" bzw. lang="en" aus
  const EN = document.documentElement.lang === "en";
  const L = (de, en) => (EN ? en : de);
  const LOCALE = EN ? "en-GB" : "de-DE";

  const LABELS = {
    offline: "OFFLINE", idle: L("BEREIT", "READY"), listening: L("HÖRE ZU", "LISTENING"),
    thinking: L("DENKE NACH", "THINKING"), speaking: L("SPRECHE", "SPEAKING"), executing: L("FÜHRE AUS", "EXECUTING"),
    confirm: L("WARTE AUF FREIGABE", "AWAITING APPROVAL"), error: L("FEHLER", "ERROR"),
  };
  const STATUS_TEXT = EN
    ? { running: "running", waiting: "waiting", ok: "done", denied: "denied", blocked: "blocked", error: "error" }
    : { running: "läuft", waiting: "wartet", ok: "fertig", denied: "abgelehnt", blocked: "blockiert", error: "fehler" };

  const store = {
    get(k, d) { try { const v = localStorage.getItem("orbwise." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("orbwise." + k, JSON.stringify(v)); } catch { /* egal */ } },
  };

  const S = {
    ws: null, connected: false, retry: 0,
    serverState: "idle", substate: "",
    tts: store.get("tts", true), wake: store.get("wake", false), think: store.get("think", false),
    voiceName: store.get("voice", ""), fxOn: store.get("fx", true), fxAmount: store.get("fxAmount", 0.6),
    recording: false, transcribing: false, streamMic: false,
    playing: false, confirm: null, confirmListenSent: false,
    historyLoaded: false, status: null, errorUntil: 0,
  };

  // ---------------------------------------------------------------- Audio
  const A = { ctx: null, outAnalyser: null, micAnalyser: null, micNode: null, micReady: false,
              queue: [], gen: 0, current: null, micLevel: 0, spec: null, synthLevel: 0 };

  async function initAudio() {
    if (A.ctx) return;
    A.ctx = new (window.AudioContext || window.webkitAudioContext)();
    A.outAnalyser = A.ctx.createAnalyser();
    A.outAnalyser.fftSize = 256;
    A.outAnalyser.connect(A.ctx.destination);
    A.spec = new Uint8Array(A.outAnalyser.frequencyBinCount);
    if (window.VoiceFX) {
      A.fx = new window.VoiceFX(A.ctx, A.outAnalyser);
      A.fx.set(S.fxOn, S.fxAmount);
    }
    if (A.ctx.state === "suspended") await A.ctx.resume();
  }

  async function initMic() {
    if (A.micReady) return true;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast(L("Mikrofon nicht verfügbar – Seite über http://localhost öffnen.", "Microphone unavailable – open the page via http://localhost."));
      return false;
    }
    try {
      await initAudio();
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      await A.ctx.audioWorklet.addModule("/static/mic-worklet.js");
      const src = A.ctx.createMediaStreamSource(stream);
      A.micNode = new AudioWorkletNode(A.ctx, "pcm-downsampler");
      A.micNode.port.onmessage = (e) => {
        A.micLevel = e.data.level;
        if (S.streamMic && S.ws && S.ws.readyState === 1) S.ws.send(e.data.pcm);
      };
      src.connect(A.micNode);
      // Worklet muss an einen Ausgang hängen, damit er läuft – stumm geschaltet
      const mute = A.ctx.createGain();
      mute.gain.value = 0;
      A.micNode.connect(mute).connect(A.ctx.destination);
      A.micReady = true;
      return true;
    } catch (err) {
      toast(L("Kein Mikrofonzugriff: ", "No microphone access: ") + err.message);
      return false;
    }
  }

  function updateMicStreaming() {
    S.streamMic = A.micReady && (S.wake || S.recording);
  }

  function b64ToBuffer(b64) {
    const bin = atob(b64);
    const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return buf.buffer;
  }

  let germanVoice = null;
  function pickVoice() {
    if (!window.speechSynthesis) return;
    const voices = speechSynthesis.getVoices();
    germanVoice = voices.find((v) => /de[-_]DE/i.test(v.lang) && /male|mann|stefan|markus/i.test(v.name))
      || voices.find((v) => /^de/i.test(v.lang)) || null;
  }
  if (window.speechSynthesis) { pickVoice(); speechSynthesis.onvoiceschanged = pickVoice; }

  function enqueueSpeech(ev) {
    if (!S.tts) return;
    A.queue.push(ev);
    if (!S.playing) playNext();
  }

  async function playNext() {
    const gen = A.gen;
    const ev = A.queue.shift();
    if (!ev) { S.playing = false; A.current = null; refresh(); onSpeechDrained(); return; }
    S.playing = true;
    refresh();
    const done = () => { if (gen === A.gen) playNext(); };
    try {
      if (ev.audio && A.ctx) {
        const buf = await A.ctx.decodeAudioData(b64ToBuffer(ev.audio));
        if (gen !== A.gen) return;
        const src = A.ctx.createBufferSource();
        src.buffer = buf;
        src.playbackRate.value = fxRate();
        src.connect(A.fx ? A.fx.input : A.outAnalyser);
        src.onended = done;
        A.current = src;
        src.start();
      } else if (window.speechSynthesis) {
        const u = new SpeechSynthesisUtterance(ev.text);
        u.lang = EN ? "en-GB" : "de-DE";
        if (germanVoice) u.voice = germanVoice;
        u.rate = 1.05;
        u.pitch = S.fxOn ? 1 - 0.3 * S.fxAmount : 1;
        u.onend = done;
        u.onerror = done;
        u.onboundary = () => { A.synthLevel = 0.6 + Math.random() * 0.4; };
        speechSynthesis.speak(u);
      } else {
        done();
      }
    } catch (err) {
      console.warn("Audio-Fehler", err);
      done();
    }
  }

  function stopSpeech(notifyServer) {
    A.gen++;
    A.queue.length = 0;
    try { if (A.current) A.current.stop(); } catch { /* schon gestoppt */ }
    A.current = null;
    if (window.speechSynthesis) speechSynthesis.cancel();
    S.playing = false;
    if (notifyServer) send({ type: "speech_interrupt" });
    refresh();
  }

  function onSpeechDrained() {
    // Nach der gesprochenen Rückfrage automatisch auf „Ja/Nein“ hören
    if (S.confirm && !S.confirmListenSent && A.micReady && (S.wake || S.voiceUsed)) {
      S.confirmListenSent = true;
      startListen();
    }
  }

  // Pegel → Orb
  function levelLoop() {
    let level = 0;
    let spec = null;
    if (S.recording) {
      level = Math.min(1, A.micLevel * 6);
    } else if (S.playing && A.current && A.outAnalyser) {
      A.outAnalyser.getByteFrequencyData(A.spec);
      spec = A.spec;
      let sum = 0;
      for (let i = 2; i < 60; i++) sum += A.spec[i];
      level = Math.min(1, sum / (58 * 160));
    } else if (S.playing) {
      A.synthLevel *= 0.93;
      level = 0.25 + A.synthLevel * 0.5;
    }
    orb.setLevel(level);
    orb.setSpectrum(spec);
    requestAnimationFrame(levelLoop);
  }
  requestAnimationFrame(levelLoop);

  // ---------------------------------------------------------------- Zustand
  function refresh() {
    let s = S.serverState;
    if (!S.connected) s = "offline";
    else if (S.confirm) s = "confirm";
    else if (S.recording) s = "listening";
    else if (S.transcribing) s = "thinking";
    else if (S.playing) s = "speaking";
    else if (Date.now() < S.errorUntil) s = "error";
    if (S.modelSwitching && S.connected) s = "thinking";
    orb.setState(s);
    const label = $("state-label");
    label.textContent = S.modelSwitching && S.connected ? L("LADE MODELL", "LOADING MODEL") : (LABELS[s] || s.toUpperCase());
    label.style.color = { listening: "#4dffb8", executing: "#ffb347", confirm: "#ffb347", error: "#ff5d6c", offline: "#6d93aa", thinking: "#9aa6ff" }[s] || "";
    let sub = S.substate;
    if (S.recording) sub = S.recordingMode === "ptt" ? L("Loslassen zum Senden", "Release to send") : L("Sprich jetzt …", "Speak now …");
    else if (S.transcribing) sub = L("Transkribiere …", "Transcribing …");
    else if (s === "idle" && S.wake) sub = L("Sag „Hey Jarvis“", "Say “Hey Jarvis”");
    $("substate-label").textContent = sub || "";
    $("btn-mic").classList.toggle("recording", S.recording);
    $("btn-wake").classList.toggle("on", S.wake);
    $("btn-tts").classList.toggle("on", S.tts);
    $("btn-think").classList.toggle("on", S.think);
  }

  // ---------------------------------------------------------------- WebSocket
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.binaryType = "arraybuffer";
    S.ws = ws;
    ws.onopen = () => {
      S.connected = true;
      S.retry = 0;
      send({ type: "tts", enabled: S.tts });
      send({ type: "think", enabled: S.think });
      sendVoiceSettings();
      if (S.wake && A.micReady) send({ type: "wake", enabled: true });
      refresh();
      loadStatus();
      getJSON("/api/metrics").then(showMetrics).catch(() => {});
      if (!S.historyLoaded) loadHistory();
    };
    ws.onclose = () => {
      S.connected = false;
      S.recording = false;
      updateMicStreaming();
      refresh();
      const delay = Math.min(10000, 800 * 2 ** S.retry++);
      setTimeout(connect, delay);
    };
    ws.onmessage = (e) => {
      if (typeof e.data !== "string") return;
      handle(JSON.parse(e.data));
    };
  }

  function send(obj) {
    if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(obj));
  }

  const THINKING_SUB = L("denkt nach …", "thinking …");

  function handle(ev) {
    switch (ev.type) {
      case "hello":
        if (ev.busy) S.serverState = "thinking";
        if (ev.context) showContext(ev.context);
        break;
      case "context":
        showContext(ev);
        return;
      case "state":
        S.serverState = ev.state === "confirm" ? S.serverState : ev.state;
        S.substate = ev.state === "executing" && ev.tool ? ev.tool : "";
        break;
      case "user":
        addUser(ev.text, ev.source);
        if (ev.source === "voice") S.voiceUsed = true;
        break;
      case "assistant_start":
        startAssistant(ev.id);
        break;
      case "token":
        appendToken(ev.id, ev.text);
        T.tokenTimes.push(performance.now());
        if (Thought.active) Thought.zoomOut();
        if (S.substate === THINKING_SUB) { S.substate = ""; break; }
        return;  // Zustand ändert sich pro Token nicht – kein refresh() nötig
      case "reasoning":
        // Denkkette: nicht in die Antwort, sondern in den Orb (hineinzoomen) und später aufklappbar im Chat
        Thought.add(ev.id, ev.text || "");
        if (S.substate !== THINKING_SUB) S.substate = THINKING_SUB;
        else return;
        break;
      case "segment_end":
        appendToken(ev.id, "\n\n");
        break;
      case "assistant_end":
        Thought.finish(ev.id);
        finishAssistant(ev.id, ev.cancelled);
        if (S.substate === THINKING_SUB) S.substate = "";
        setTimeout(loadStatus, 300);
        if (!$("chat-title").textContent) {
          getJSON("/api/chats").then((l) => {
            const c = l.find((x) => x.active);
            if (c && c.messages) $("chat-title").textContent = "· " + c.title;
          }).catch(() => {});
        }
        if (!$("tab-chats").classList.contains("hidden")) loadChats();
        if (!$("tab-memory").classList.contains("hidden")) loadReminders();
        break;
      case "tool_call":
        if (Thought.active) Thought.zoomOut();
        toolCall(ev);
        break;
      case "tool_output":
        toolOutput(ev);
        break;
      case "tool_result":
        toolResult(ev);
        break;
      case "confirm_request":
        openConfirm(ev);
        break;
      case "confirm_done":
        closeConfirm(ev.id);
        break;
      case "password_request":
        openPassword(ev);
        break;
      case "password_done":
        closePassword(ev.id);
        break;
      case "speak":
        enqueueSpeech(ev);
        break;
      case "audio_stop":
        stopSpeech(false);
        break;
      case "voice":
        voiceEvent(ev);
        break;
      case "transcript":
        showTranscript(ev.text);
        break;
      case "error":
        addError(ev.text);
        S.errorUntil = Date.now() + 2500;
        setTimeout(refresh, 2600);
        break;
      case "memory":
        addSystem(ev.text);
        break;
      case "reminder":
        showReminder(ev);
        break;
      case "model_switching":
        S.modelSwitching = ev.label || ev.name;
        S.substate = L("Wechsle zu ", "Switching to ") + `${ev.label || ev.name} …`;
        setPill("pill-llm", "warn", L("lädt …", "loading …"));
        break;
      case "model_progress":
        S.substate = ev.text;
        break;
      case "model_active":
        if (S.modelSwitching) addSystem(L("Modell aktiv: ", "Model active: ") + S.modelSwitching);
        S.modelSwitching = null;
        S.substate = "";
        loadStatus();
        break;
      case "model_pull":
        modelPullEvent(ev);
        return;
      case "model_error":
        S.modelSwitching = null;
        S.substate = "";
        addError(L("Modellwechsel fehlgeschlagen: ", "Model switch failed: ") + ev.text);
        loadStatus();
        break;
      case "metrics":
        showMetrics(ev);
        break;
      case "llm_stats":
        T.exactTps = ev.tps;
        T.lastExact = Date.now();
        T.tokenTimes = [];
        setTile("tps", ev.tps, { sub: `${ev.tokens || "?"} Tok · Prompt ${fmt(ev.prompt_tps, 0)}/s` });
        pushSpark("tps", ev.tps);
        break;
      case "conversation_reset":
        break;  // Anzeige erledigt chat_switched
      case "chat_switched":
        openChatView(ev);
        break;
      case "chats_changed":
        if (!$("tab-chats").classList.contains("hidden")) loadChats();
        return;
    }
    refresh();
  }

  function voiceEvent(ev) {
    switch (ev.state) {
      case "wake":
        stopSpeech(true);
        showTranscript("…");
        break;
      case "recording":
        S.recording = true;
        S.transcribing = false;
        break;
      case "transcribing":
        S.recording = false;
        S.transcribing = true;
        break;
      case "timeout":
      case "idle":
        S.recording = false;
        S.transcribing = false;
        if (ev.state === "timeout") showTranscript("");
        break;
      case "error":
        S.recording = false;
        S.transcribing = false;
        toast(ev.text);
        break;
      case "wake_off":
        if (S.wake) { S.wake = false; store.set("wake", false); }
        break;
    }
    updateMicStreaming();
  }

  // ---------------------------------------------------------------- Chat
  const chat = $("chat");
  const assistants = {};

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function renderMarkdown(src) {
    const blocks = [];
    let text = src.replace(/```[\w-]*\n?([\s\S]*?)(```|$)/g, (_, code) => {
      blocks.push(`<pre>${escapeHtml(code.replace(/\n$/, ""))}</pre>`);
      return `\u0000${blocks.length - 1}\u0000`;
    });
    text = escapeHtml(text)
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
    const html = text.split(/\n{2,}/).map((para) => {
      if (/^\u0000\d+\u0000$/.test(para.trim())) return para.trim();
      const lines = para.split("\n");
      if (lines.every((l) => /^\s*([-*•]|\d+\.)\s+/.test(l) || !l.trim())) {
        return "<ul>" + lines.filter((l) => l.trim()).map((l) => `<li>${l.replace(/^\s*([-*•]|\d+\.)\s+/, "")}</li>`).join("") + "</ul>";
      }
      return `<p>${lines.join("<br>")}</p>`;
    }).join("");
    return html.replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
  }

  function scrollChat() { chat.scrollTop = chat.scrollHeight; }

  function addMsg(cls, who, html) {
    const el = document.createElement("div");
    el.className = "msg " + cls;
    el.innerHTML = `<div class="who">${who}</div><div class="tools"></div><div class="body">${html}</div>`;
    chat.appendChild(el);
    scrollChat();
    return el;
  }

  function addUser(text, source) {
    addMsg("user", source === "voice" ? L("DU · SPRACHE", "YOU · VOICE") : L("DU", "YOU"), escapeHtml(text));
  }
  function addSystem(text) {
    const el = document.createElement("div");
    el.className = "msg system";
    el.textContent = text;
    chat.appendChild(el);
    scrollChat();
  }
  function addError(text) { addMsg("assistant error", "SYSTEM", escapeHtml(text)); }

  function startAssistant(id) {
    const el = addMsg("assistant streaming", "JARVIS", "");
    assistants[id] = { el, raw: "" };
    S.currentMsg = id;
  }
  // Token werden gesammelt und höchstens einmal pro Bild (~16–60 ms) als Markdown gerendert –
  // vorher wurde bei jedem Token die ganze Antwort neu aufgebaut (bei langen Antworten sehr teuer).
  const dirty = new Set();
  let renderQueued = false;
  function renderAssistant(a) {
    a.el.querySelector(".body").innerHTML = renderMarkdown(a.raw.replace(/\n{3,}/g, "\n\n"));
  }
  function flushTokens() {
    renderQueued = false;
    if (!dirty.size) return;
    const stick = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80;
    for (const a of dirty) renderAssistant(a);
    dirty.clear();
    if (stick) scrollChat();
  }
  function appendToken(id, text) {
    const a = assistants[id] || (startAssistant(id), assistants[id]);
    a.raw += text;
    dirty.add(a);
    if (!renderQueued) {
      renderQueued = true;
      // rAF, mit Timeout-Fallback falls der Tab im Hintergrund ist
      let done = false;
      const go = () => { if (!done) { done = true; flushTokens(); } };
      requestAnimationFrame(go);
      setTimeout(go, 100);
    }
  }
  // Denkmodus: Gedankengang im Orb anzeigen (Zoom per CSS), danach aufklappbar an der Antwort
  const Thought = {
    active: false, text: "", byMsg: {}, queued: false, since: 0, outTimer: null, warned: false,
    add(id, text) {
      if (!text) return;
      this.byMsg[id] = (this.byMsg[id] || "") + text;
      if (!this.active) {
        this.active = true;
        this.since = performance.now();
        this.text = "";
        clearTimeout(this.outTimer);
        $("thought-text").textContent = "";
        document.querySelector(".core").classList.add("zoomed");
      }
      this.text += text;
      if (!this.queued) {
        this.queued = true;
        let done = false;
        const go = () => {
          if (done) return;
          done = true;
          this.queued = false;
          const el = $("thought-text");
          el.textContent = this.text.length > 6000 ? "…" + this.text.slice(-6000) : this.text;
          el.scrollTop = el.scrollHeight;
        };
        requestAnimationFrame(go);
        setTimeout(go, 120);
      }
    },
    zoomOut() {
      if (!this.active) return;
      this.active = false;
      // kurz stehen lassen, damit das Zoomen nicht flackert
      const wait = Math.max(0, 700 - (performance.now() - this.since));
      clearTimeout(this.outTimer);
      this.outTimer = setTimeout(() => document.querySelector(".core").classList.remove("zoomed"), wait);
    },
    finish(id) {
      this.zoomOut();
      const text = (this.byMsg[id] || "").trim();
      delete this.byMsg[id];
      const a = assistants[id];
      if (text && a) {
        const d = document.createElement("details");
        d.className = "thought-log";
        d.innerHTML = `<summary>${L("GEDANKENGANG", "THOUGHTS")}</summary><pre></pre>`;
        d.querySelector("pre").textContent = text;
        a.el.insertBefore(d, a.el.querySelector(".body"));
      } else if (!text && S.think && !this.warned) {
        this.warned = true;
        toast(L("Denkmodus an, aber das Modell hat keinen Gedankengang geliefert – bei Bonsai '--reasoning-budget 0' aus dem Startbefehl entfernen.",
              "Thinking mode is on, but the model returned no reasoning – for llama-server remove '--reasoning-budget 0' from the start command."));
      }
    },
  };

  function finishAssistant(id, cancelled) {
    const a = assistants[id];
    if (!a) return;
    if (dirty.has(a)) { dirty.delete(a); renderAssistant(a); scrollChat(); }
    a.el.classList.remove("streaming");
    if (!a.raw.trim()) {
      a.el.querySelector(".body").innerHTML = cancelled ? "<em>(abgebrochen)</em>" : "";
      if (!cancelled && !a.el.querySelector(".tool-chip")) a.el.remove();
    }
    delete assistants[id];
  }

  function showTranscript(text) {
    const el = $("live-transcript");
    el.textContent = text ? L(`„${text}“`, `“${text}”`) : "";
    el.style.opacity = 1;
    clearTimeout(showTranscript.t);
    showTranscript.t = setTimeout(() => { el.style.opacity = 0; }, 5000);
  }

  // ---------------------------------------------------------------- Aktivität
  const activity = $("activity");
  const acts = {};

  function fmtArgs(name, args) {
    if (name === "run_shell") return args.command || "";
    const entries = Object.entries(args || {});
    return entries.map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join("  ");
  }

  function toolCall(ev) {
    const empty = activity.querySelector(".empty");
    if (empty) empty.remove();
    const el = document.createElement("div");
    const waiting = ev.risk === "confirm";
    const status = ev.risk === "blocked" ? "blocked" : waiting ? "waiting" : "running";
    el.className = "act running";
    el.innerHTML = `<div class="act-head"><span class="act-name">${escapeHtml(ev.name)}</span>
      <span><span class="act-time">${new Date().toLocaleTimeString(LOCALE)}</span>
      <span class="act-status">${STATUS_TEXT[status]}</span>
      <button class="toggle-out" title="${L("Ausgabe ein-/ausblenden", "Show/hide output")}">▾</button></span></div>
      <div class="act-args"></div><pre class="act-out"></pre>`;
    el.querySelector(".act-args").textContent = fmtArgs(ev.name, ev.args);
    el.querySelector(".toggle-out").onclick = () => el.classList.toggle("open");
    activity.prepend(el);
    acts[ev.id] = el;
    while (activity.children.length > 60) activity.lastChild.remove();

    const a = S.currentMsg && assistants[S.currentMsg];
    if (a) {
      const chip = document.createElement("span");
      chip.className = "tool-chip running";
      chip.id = "chip-" + ev.id;
      chip.textContent = "⚙ " + ev.name;
      a.el.querySelector(".tools").appendChild(chip);
    }
  }

  function toolOutput(ev) {
    const el = acts[ev.id];
    if (!el) return;
    const out = el.querySelector(".act-out");
    out.textContent = (out.textContent + ev.text).slice(-20000);
    out.scrollTop = out.scrollHeight;
  }

  function toolResult(ev) {
    const el = acts[ev.id];
    if (el) {
      el.className = "act " + ev.status;
      el.querySelector(".act-status").textContent = STATUS_TEXT[ev.status] || ev.status;
      const out = el.querySelector(".act-out");
      if (!out.textContent.trim() || ev.status !== "ok") out.textContent = ev.text;
    }
    const chip = $("chip-" + ev.id);
    if (chip) chip.className = "tool-chip " + ev.status;
  }

  // ---------------------------------------------------------------- Bestätigung
  function openConfirm(ev) {
    S.confirm = ev;
    S.confirmListenSent = false;
    $("confirm-summary").textContent = L(`Soll ich ${ev.summary} ausführen?`, `Shall I run ${ev.summary}?`);
    $("confirm-cmd").textContent = ev.name === "run_shell" ? ev.args.command : `${ev.name}(${JSON.stringify(ev.args, null, 2)})`;
    $("confirm-reason").textContent = ev.reason ? "Grund: " + ev.reason : "";
    $("confirm-voice").textContent = A.micReady ? L("oder sag „Ja“ bzw. „Nein“", "or say “yes” or “no”") : "";
    $("confirm-voice").classList.remove("listening");
    $("confirm").classList.remove("hidden");
    const act = acts[ev.id];
    if (act) act.querySelector(".act-status").textContent = STATUS_TEXT.waiting;
    setTimeout(() => $("confirm-yes").focus(), 50);
    refresh();
    if (!S.tts) onSpeechDrained();
  }

  function closeConfirm(id) {
    if (S.confirm && (!id || S.confirm.id === id)) {
      S.confirm = null;
      $("confirm").classList.add("hidden");
      if (S.recording) send({ type: "cancel_listen" });
      refresh();
    }
  }

  function answerConfirm(approved) {
    if (!S.confirm) return;
    send({ type: "confirm", id: S.confirm.id, approved });
    const act = acts[S.confirm.id];
    if (act && approved) act.querySelector(".act-status").textContent = STATUS_TEXT.running;
    closeConfirm();
  }
  $("confirm-yes").onclick = () => answerConfirm(true);
  $("confirm-no").onclick = () => answerConfirm(false);

  // ---------------------------------------------------------------- Mikrofon
  let pttStart = 0;
  let pttActive = false;

  async function startPtt() {
    if (pttActive) return;
    if (!(await initMic())) return;
    pttActive = true;
    pttStart = performance.now();
    stopSpeech(true);
    S.recording = true;
    S.recordingMode = "ptt";
    S.voiceUsed = true;
    updateMicStreaming();
    send({ type: "ptt_start" });
    refresh();
  }

  function endPtt() {
    if (!pttActive) return;
    pttActive = false;
    if (performance.now() - pttStart < 280) {
      // Kurz angetippt → zuhören, bis Stille erkannt wird
      startListen();
    } else {
      send({ type: "ptt_stop" });
    }
  }

  async function startListen() {
    if (!(await initMic())) return;
    S.recording = true;
    S.recordingMode = "vad";
    updateMicStreaming();
    send({ type: "listen" });
    if (S.confirm) {
      $("confirm-voice").textContent = L("Ich höre … sag „Ja“ oder „Nein“", "Listening … say “yes” or “no”");
      $("confirm-voice").classList.add("listening");
    }
    refresh();
  }

  const mic = $("btn-mic");
  mic.addEventListener("pointerdown", (e) => { e.preventDefault(); startPtt(); });
  mic.addEventListener("pointerup", endPtt);
  mic.addEventListener("pointerleave", () => { if (pttActive) endPtt(); });

  // ---------------------------------------------------------------- Passwort für sudo (Root-Rechte)
  let pwId = null;
  function openPassword(ev) {
    pwId = ev.id;
    $("pw-prompt").textContent = ev.retry ? L("Falsches Passwort – bitte erneut eingeben", "Wrong password – please try again")
                                       : L("Root-Passwort (sudo)", "Root password (sudo)");
    $("pw-cmd").textContent = ev.command || "";
    $("pw-input").value = "";
    $("pw-modal").classList.remove("hidden");
    setTimeout(() => $("pw-input").focus(), 30);
    if (ev.retry) toast(L("Falsches Passwort – bitte erneut eingeben.", "Wrong password – please try again."));
  }
  function closePassword(id) {
    if (id && id !== pwId) return;
    pwId = null;
    $("pw-input").value = "";
    $("pw-modal").classList.add("hidden");
  }
  $("pw-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (!pwId) return;
    const password = $("pw-input").value;
    $("pw-input").value = "";  // nicht im DOM stehen lassen
    send(password ? { type: "password", id: pwId, password } : { type: "password_cancel", id: pwId });
    closePassword();
  });
  $("pw-cancel").onclick = () => {
    if (pwId) send({ type: "password_cancel", id: pwId });
    closePassword();
  };
  $("pw-input").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); $("pw-cancel").click(); }
  });

  document.addEventListener("keydown", (e) => {
    if (!$("boot").classList.contains("hidden")) return;
    if (pwId) return;  // Passwortfeld hat Vorrang (kein Push-to-talk mit Leertaste)
    if (S.confirm) {
      if (e.key === "Enter") { e.preventDefault(); answerConfirm(true); }
      if (e.key === "Escape") { e.preventDefault(); answerConfirm(false); }
      return;
    }
    if (e.key === "Escape") {
      if (!$("day-modal").classList.contains("hidden")) { $("day-modal").classList.add("hidden"); return; }
      send({ type: "stop" });
      stopSpeech(true);
      return;
    }
    if (e.code === "Space" && !e.repeat && document.activeElement !== $("input")) {
      e.preventDefault();
      startPtt();
    }
  });
  document.addEventListener("keyup", (e) => {
    if (e.code === "Space" && pttActive) { e.preventDefault(); endPtt(); }
  });

  // ---------------------------------------------------------------- Bedienelemente
  $("form").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("input").value.trim();
    if (!text) return;
    if (!S.connected) { toast(L("Keine Verbindung zum Server.", "No connection to the server.")); return; }
    send({ type: "user_message", text });
    $("input").value = "";
  });

  $("btn-think").onclick = () => {
    S.think = !S.think;
    store.set("think", S.think);
    send({ type: "think", enabled: S.think });
    toast(S.think ? L("Denkmodus an – Antworten dauern länger, der Gedankengang erscheint im Orb.",
                      "Thinking mode on – answers take longer, the reasoning appears in the orb.")
                  : L("Denkmodus aus – schnelle Antworten.", "Thinking mode off – fast answers."));
    refresh();
  };

  $("btn-tts").onclick = () => {
    S.tts = !S.tts;
    store.set("tts", S.tts);
    send({ type: "tts", enabled: S.tts });
    if (!S.tts) stopSpeech(true);
    refresh();
  };

  $("btn-wake").onclick = async () => {
    const enable = !S.wake;
    if (enable) {
      if (S.status && !S.status.voice.wake) { toast(S.status.voice.wake_error || L("Wake-Word ist nicht verfügbar.", "Wake word is not available.")); return; }
      if (!(await initMic())) return;
    }
    S.wake = enable;
    store.set("wake", enable);
    updateMicStreaming();
    send({ type: "wake", enabled: enable });
    refresh();
  };

  $("btn-stop").onclick = () => { send({ type: "stop" }); stopSpeech(true); };
  $("btn-reset").onclick = () => newChat();

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.onclick = () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      $("tab-activity").classList.toggle("hidden", tab.dataset.tab !== "activity");
      $("tab-chats").classList.toggle("hidden", tab.dataset.tab !== "chats");
      if (tab.dataset.tab === "chats") loadChats();
      $("tab-memory").classList.toggle("hidden", tab.dataset.tab !== "memory");
      $("tab-voice").classList.toggle("hidden", tab.dataset.tab !== "voice");
      $("tab-briefing").classList.toggle("hidden", tab.dataset.tab !== "briefing");
      if (tab.dataset.tab === "briefing") loadBriefing();
      if (tab.dataset.tab === "memory") loadMemory();
      if (tab.dataset.tab === "voice") loadVoices();
    };
  });

  // ---------------------------------------------------------------- Stimme & Effekt
  function fxRate() {
    return S.fxOn ? +(1 - 0.08 * S.fxAmount).toFixed(3) : 1;
  }

  function sendVoiceSettings() {
    send({ type: "voice_settings", voice: S.voiceName || undefined, rate: fxRate() });
  }

  function renderFx() {
    $("fx-toggle").textContent = S.fxOn ? "AN" : "AUS";
    $("fx-toggle").classList.toggle("on", S.fxOn);
    $("fx-amount").value = Math.round(S.fxAmount * 100);
    $("fx-value").textContent = Math.round(S.fxAmount * 100) + " %";
    if (A.fx) A.fx.set(S.fxOn, S.fxAmount);
  }

  $("fx-toggle").onclick = () => {
    S.fxOn = !S.fxOn;
    store.set("fx", S.fxOn);
    renderFx();
    sendVoiceSettings();
  };
  $("fx-amount").oninput = (e) => {
    S.fxAmount = e.target.value / 100;
    store.set("fxAmount", S.fxAmount);
    renderFx();
  };
  $("fx-amount").onchange = () => sendVoiceSettings();
  renderFx();

  async function loadVoices() {
    const list = $("voices");
    let data;
    try {
      data = await getJSON("/api/voices");
    } catch {
      list.innerHTML = `<li class="empty">${L("Stimmen nicht ladbar.", "Could not load voices.")}</li>`;
      return;
    }
    if (!data.available) {
      list.innerHTML = `<li class="empty">${L("Piper-Sprachausgabe ist deaktiviert – es spricht der Browser.", "Piper speech output is disabled – the browser speaks instead.")}</li>`;
      return;
    }
    if (!S.voiceName) S.voiceName = data.current;
    list.innerHTML = "";
    for (const v of data.voices) {
      const li = document.createElement("li");
      const current = v.name === data.current;
      li.className = "voice" + (current ? " current" : "");
      li.innerHTML = `<div class="voice-head"><span class="voice-name"></span><span class="voice-tag">${current ? L("AKTIV", "ACTIVE") : v.installed ? L("INSTALLIERT", "INSTALLED") : v.male ? L("MÄNNLICH", "MALE") : L("WEIBLICH", "FEMALE")}</span></div>
        <div class="voice-desc"></div><div class="voice-actions"></div>`;
      li.querySelector(".voice-name").textContent = v.label;
      li.querySelector(".voice-desc").textContent = v.description;
      const actions = li.querySelector(".voice-actions");
      const btn = (label, fn) => {
        const b = document.createElement("button");
        b.className = "ghost";
        b.textContent = label;
        b.onclick = async () => { b.disabled = true; try { await fn(b); } finally { b.disabled = false; } };
        actions.appendChild(b);
        return b;
      };
      if (v.installed) {
        btn(L("ANHÖREN", "PREVIEW"), () => previewVoice(v.name));
        if (!current) btn(L("AUSWÄHLEN", "SELECT"), async () => {
          S.voiceName = v.name;
          store.set("voice", v.name);
          sendVoiceSettings();
          setTimeout(loadVoices, 150);
        });
      } else {
        btn(L("INSTALLIEREN", "INSTALL"), async (b) => {
          b.textContent = L("LÄDT …", "LOADING …");
          const r = await fetch(`/api/voices/${encodeURIComponent(v.name)}/install`, { method: "POST" });
          if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            toast(err.detail || L("Installation fehlgeschlagen", "Installation failed"));
          }
          loadVoices();
          loadStatus();
        });
      }
      list.appendChild(li);
    }
  }

  async function previewVoice(name) {
    await initAudio();
    sendVoiceSettings();
    const r = await fetch(`/api/voices/${encodeURIComponent(name)}/preview`);
    if (!r.ok) { toast(L("Probe nicht möglich", "Preview not possible")); return; }
    const buf = await A.ctx.decodeAudioData(await r.arrayBuffer());
    stopSpeech(true);
    const src = A.ctx.createBufferSource();
    src.buffer = buf;
    src.playbackRate.value = fxRate();
    src.connect(A.fx ? A.fx.input : A.outAnalyser);
    A.current = src;
    S.playing = true;
    refresh();
    src.onended = () => { S.playing = false; A.current = null; refresh(); };
    src.start();
  }

  // ---------------------------------------------------------------- Telemetrie
  const T = { tokenTimes: [], exactTps: null, lastExact: 0, spark: {} };
  const SPARK_MAX = { gpu: 100, power: null, vram: null, ram: null, tps: null, ctx: 100 };

  // Kontext-Budget: genutzte (geschätzte) Prompt-Token im Verhältnis zum Budget des Modells
  function kTok(n) {
    return n >= 1000 ? `${fmt(n / 1000, n >= 10000 ? 0 : 1)}k` : String(n);
  }
  function showContext(c) {
    if (!c || !c.budget) return;
    const pct = (100 * c.used) / c.budget;
    const p = c.parts || {};
    setTile("ctx", c.used / 1000, {
      pct,
      digits: 1,
      sub: `${Math.round(pct)} %` + (c.trimmed ? L(" · gekürzt", " · trimmed") : ""),
      title: [
        L(`Prompt ca. ${c.used} von ${c.budget} Token Budget (Fenster ${c.window}, Rest bleibt für die Antwort)`,
          `Prompt approx. ${c.used} of ${c.budget} token budget (window ${c.window}, the rest is kept for the answer)`),
        `System ${p.system ?? "?"} · Tools ${p.tools ?? "?"} · ${L("Gedächtnis", "Memory")} ${p.memory ?? "?"} · `
          + `${L("Verlauf", "History")} ${p.history ?? "?"}`,
        c.real ? L("Laut Modell-Server: ", "According to the model server: ") + `${c.real} Token` : "",
        c.trimmed ? L("Ältere Teile/lange Tool-Ergebnisse wurden gekürzt, damit alles passt.",
                      "Older parts/long tool results were trimmed so everything fits.") : "",
      ].filter(Boolean).join("\n"),
    });
    $("tele-ctx").querySelector(".tele-num").textContent = kTok(c.used);
    $("tele-ctx").querySelector(".tele-unit").textContent = "/" + Math.round(c.budget / 1000) + "k";
    $("tele-ctx").classList.toggle("warn", c.trimmed || (pct >= 80 && pct < 95));
    pushSpark("ctx", Math.min(100, pct));
  }

  function fmt(v, digits = 1) {
    return v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toLocaleString(LOCALE, {
      minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function setTile(key, value, { sub = "", pct = null, digits = 1, title = "" } = {}) {
    const el = $("tele-" + key);
    if (!el) return;
    el.querySelector(".tele-num").textContent = fmt(value, digits);
    el.querySelector(".tele-sub").textContent = sub;
    el.classList.toggle("na", value === null || value === undefined);
    el.classList.toggle("warn", pct !== null && pct >= 80 && pct < 95);
    el.classList.toggle("crit", pct !== null && pct >= 95);
    el.querySelector(".tele-bar i").style.width = pct === null ? "0" : Math.min(100, pct) + "%";
    if (title) el.title = title;
  }

  function pushSpark(key, value) {
    if (value === null || value === undefined) return;
    const arr = (T.spark[key] = T.spark[key] || []);
    arr.push(value);
    if (arr.length > 30) arr.shift();
    const canvas = document.querySelector(`#tele-${key} .tele-spark`);
    if (!canvas || canvas.offsetParent === null) return;
    const ctx = canvas.getContext("2d");
    const w = canvas.width, h = canvas.height;
    const max = SPARK_MAX[key] || Math.max(...arr) * 1.15 || 1;
    ctx.clearRect(0, 0, w, h);
    ctx.beginPath();
    arr.forEach((v, i) => {
      const x = (i / 29) * w, y = h - 2 - (v / max) * (h - 4);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    const tile = canvas.closest(".tele");
    const color = tile.classList.contains("crit") ? "#ff5d6c" : tile.classList.contains("warn") ? "#ffb347" : "#3fd0ff";
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.lineTo(((arr.length - 1) / 29) * w, h);
    ctx.lineTo(0, h);
    ctx.globalAlpha = 0.15;
    ctx.fillStyle = color;
    ctx.fill();
    ctx.globalAlpha = 1;
  }

  function showMetrics(m) {
    const g = m.gpu;
    const gb = (b) => (b === null || b === undefined ? null : b / 1024 ** 3);
    if (g) {
      setTile("gpu", g.util, { pct: g.util, digits: 0, sub: g.temp !== null && g.temp !== undefined ? `${fmt(g.temp, 0)} °C` : "", title: g.name });
      const vp = g.vram_total ? (100 * g.vram_used) / g.vram_total : null;
      setTile("vram", gb(g.vram_used), { pct: vp, sub: `${L("von", "of")} ${fmt(gb(g.vram_total))} GB`, title: g.name });
      setTile("power", g.power, { digits: 0, sub: g.name ? g.name.replace(/^(NVIDIA|AMD)\s+/i, "") : "", title: g.name });
      pushSpark("gpu", g.util);
      pushSpark("vram", gb(g.vram_used));
      pushSpark("power", g.power);
    } else {
      setTile("gpu", null, { sub: L("keine GPU erkannt", "no GPU detected") });
      setTile("vram", null);
      setTile("power", null);
    }
    if (m.ram) {
      setTile("ram", gb(m.ram.used), { pct: (100 * m.ram.used) / m.ram.total,
        sub: `${L("von", "of")} ${fmt(gb(m.ram.total))} GB` + (m.cpu !== null && m.cpu !== undefined ? ` · CPU ${fmt(m.cpu, 0)} %` : "") });
      pushSpark("ram", gb(m.ram.used));
    }
  }

  // Live-Token/s während des Streamens (gleitendes 2-s-Fenster), danach exakter Ollama-Wert
  setInterval(() => {
    const now = performance.now();
    T.tokenTimes = T.tokenTimes.filter((t) => now - t < 2000);
    const streaming = Object.keys(assistants).length > 0;
    if (streaming && T.tokenTimes.length >= 3 && Date.now() - T.lastExact > 1500) {
      const span = (now - T.tokenTimes[0]) / 1000 || 1;
      const live = T.tokenTimes.length / Math.max(span, 0.25);
      setTile("tps", live, { sub: "live" });
    }
  }, 500);
  setTile("tps", null, { sub: L("wartet auf Antwort", "waiting for an answer") });

  // ---------------------------------------------------------------- Modellauswahl
  const modelMenu = $("model-menu");
  async function openModelMenu() {
    let data;
    try { data = await getJSON("/api/models"); } catch { toast(L("Modelle nicht ladbar", "Could not load models")); return; }
    modelMenu.innerHTML = `<div class="mm-title">${L("MODELL WÄHLEN", "CHOOSE MODEL")}</div>`;
    for (const p of data.profiles) {
      const b = document.createElement("button");
      b.className = "model-item" + (p.active ? " active" : "");
      b.disabled = !!data.switching;
      b.innerHTML = `<div class="mi-head"><span class="mi-name"></span><span class="mi-tag"></span></div><div class="mi-sub"></div>`;
      b.querySelector(".mi-name").textContent = p.label;
      b.querySelector(".mi-tag").textContent = p.active ? L("AKTIV", "ACTIVE")
        : p.managed ? L("STARTET SERVER", "STARTS SERVER") : p.backend.toUpperCase();
      b.querySelector(".mi-sub").textContent = `${p.backend} · ${p.model}`;
      b.onclick = async () => {
        closeModelMenu();
        if (p.active) return;
        const r = await fetch(`/api/models/${encodeURIComponent(p.name)}/activate`, { method: "POST" });
        if (!r.ok && r.status !== 502) toast(L("Umschalten fehlgeschlagen", "Switching failed"));
      };
      modelMenu.appendChild(b);
    }
    if (data.active !== "demo") {
      const add = document.createElement("button");
      add.className = "model-item add";
      add.textContent = L("+ MODELL HINZUFÜGEN …", "+ ADD MODEL …");
      add.onclick = (e) => { e.stopPropagation(); openPresetMenu(); };
      modelMenu.appendChild(add);
    }
    modelMenu.classList.remove("hidden");
    $("pill-llm").setAttribute("aria-expanded", "true");
  }
  // Vorauswahl bekannter Ollama-Modelle: passend zum Grafikspeicher, Laden mit Fortschritt (model_pull-Events)
  async function openPresetMenu() {
    let data;
    try { data = await getJSON("/api/models/presets"); } catch { toast(L("Vorauswahl nicht ladbar", "Could not load presets")); return; }
    const g = data.gpu;
    const gpuText = g.vendor === "none" ? L("keine GPU erkannt", "no GPU detected") : `${g.name || g.vendor} · ${g.vram_gb} GB`;
    modelMenu.innerHTML = `<div class="mm-title">${L("MODELL HINZUFÜGEN", "ADD MODEL")}</div>
      <div class="mm-hint"></div>`;
    modelMenu.querySelector(".mm-hint").textContent = gpuText + " · " +
      L("✔ passt · ~ teils im RAM (langsamer) · ✘ zu groß", "✔ fits · ~ partly in RAM (slower) · ✘ too big");
    const marks = { ok: "✔", tight: "~", big: "✘" };
    for (const p of data.presets) {
      const b = document.createElement("button");
      b.className = "model-item";
      const pulling = data.pulling.includes(p.tag);
      b.innerHTML = `<div class="mi-head"><span><span class="fit-${p.fit}">${marks[p.fit]}</span> <span class="mi-name"></span></span>
        <span class="mi-tag"></span></div><div class="mi-sub"></div><div class="mi-note"></div>`;
      b.querySelector(".mi-name").textContent = p.label;
      b.querySelector(".mi-tag").textContent = pulling ? L("LÄDT …", "LOADING …")
        : p.installed ? L("INSTALLIERT", "INSTALLED") : p.recommended ? L("EMPFOHLEN", "RECOMMENDED") : "";
      b.querySelector(".mi-sub").textContent = (p.kind === "ollama" ? p.tag : L("eigener Server · Terminal", "own server · terminal"))
        + ` · ~${p.download_gb} GB`;
      b.querySelector(".mi-note").textContent = p.note;
      b.disabled = pulling;
      b.onclick = async (e) => {
        e.stopPropagation();
        if (p.kind !== "ollama") {
          toast(L(`${p.label}: Einrichtung im Terminal mit „orbwise model add ${p.tag}“ (lädt ~7 GB und den passenden llama.cpp-Server).`,
                  `${p.label}: set it up in a terminal with “orbwise model add ${p.tag}” (downloads ~7 GB and the matching llama.cpp server).`));
          return;
        }
        if (p.fit === "big" && !confirm(L(`${p.label} ist für deinen Grafikspeicher zu groß und wird sehr langsam. Trotzdem laden?`,
                                          `${p.label} is too big for your video memory and will be very slow. Download anyway?`))) return;
        closeModelMenu();
        try {
          await api("POST", "/api/models/pull", { tag: p.tag });
          toast(L(`Lade ${p.tag} …`, `Downloading ${p.tag} …`));
        } catch { /* Meldung kommt von api() */ }
      };
      modelMenu.appendChild(b);
    }
    const back = document.createElement("button");
    back.className = "model-item add";
    back.textContent = L("← ZURÜCK", "← BACK");
    back.onclick = (e) => { e.stopPropagation(); openModelMenu(); };
    modelMenu.appendChild(back);
  }

  function modelPullEvent(ev) {
    if (ev.error) { toast(L(`✘ ${ev.tag}: `, `✘ ${ev.tag}: `) + ev.error); return; }
    if (ev.done) {
      toast(L(`✔ ${ev.tag} geladen – jetzt im Modell-Menü auswählbar.`, `✔ ${ev.tag} downloaded – now selectable in the model menu.`));
      addSystem(L(`Modell ${ev.tag} ist bereit (Menü LLM oben).`, `Model ${ev.tag} is ready (LLM menu at the top).`));
      return;
    }
    const pct = ev.total ? ` ${Math.floor((100 * (ev.completed || 0)) / ev.total)} %` : "";
    toast(L(`Lade ${ev.tag}: `, `Downloading ${ev.tag}: `) + (ev.status || "") + pct);
  }

  function closeModelMenu() {
    modelMenu.classList.add("hidden");
    $("pill-llm").setAttribute("aria-expanded", "false");
  }
  $("pill-llm").onclick = (e) => {
    e.stopPropagation();
    modelMenu.classList.contains("hidden") ? openModelMenu() : closeModelMenu();
  };
  document.addEventListener("click", (e) => { if (!modelMenu.contains(e.target)) closeModelMenu(); });

  let toastTimer;
  function toast(text) {
    const el = $("toast");
    el.textContent = text;
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 5000);
  }

  // ---------------------------------------------------------------- REST
  // ---------------------------------------------------------------- Briefing-Einstellungen
  const B = { sections: [], settings: null, timer: null };

  function renderBriefing() {
    const s = B.settings;
    const order = [...s.sections, ...B.sections.map((x) => x.id).filter((id) => !s.sections.includes(id))];
    const list = $("brief-list");
    list.innerHTML = "";
    order.forEach((id, i) => {
      const info = B.sections.find((x) => x.id === id);
      const on = s.sections.includes(id);
      const li = document.createElement("li");
      li.className = "brief-item" + (on ? "" : " off");
      li.innerHTML = `<label><input type="checkbox" ${on ? "checked" : ""}> ${escapeHtml(info.label)}`
        + (info.note ? ` <span class="brief-note">– ${escapeHtml(info.note)}</span>` : "") + `</label>`
        + `<button class="ghost" data-move="-1" title="${L("nach oben", "move up")}" ${i === 0 ? "disabled" : ""}>▲</button>`
        + `<button class="ghost" data-move="1" title="${L("nach unten", "move down")}" ${i === order.length - 1 ? "disabled" : ""}>▼</button>`;
      li.querySelector("input").onchange = (e) => {
        const cur = order.filter((x) => x === id ? e.target.checked : s.sections.includes(x));
        saveBriefing({ sections: cur });
      };
      li.querySelectorAll("button").forEach((b) => b.onclick = () => {
        const j = i + Number(b.dataset.move);
        [order[i], order[j]] = [order[j], order[i]];
        saveBriefing({ sections: order.filter((x) => s.sections.includes(x)) });
      });
      list.appendChild(li);
    });
    $("brief-days").value = s.lookahead_days;
    $("brief-topics").value = s.news_topics.join(", ");
    $("brief-count").value = s.news_count;
    $("brief-inbox").value = s.inbox_tag;
    $("brief-instr").value = s.instructions;
  }

  async function loadBriefing() {
    try {
      const data = await getJSON("/api/briefing");
      B.sections = data.sections;
      B.settings = data.settings;
      $("brief-status").textContent = data.customized ? L("im Dashboard angepasst", "customised in the dashboard")
                                                     : L("aus der Config", "from the config file");
      renderBriefing();
    } catch { $("brief-status").textContent = L("Laden fehlgeschlagen", "Loading failed"); }
  }

  async function saveBriefing(patch) {
    B.settings = { ...B.settings, ...patch };
    renderBriefing();
    try {
      const r = await fetch("/api/briefing", { method: "PUT", headers: { "Content-Type": "application/json" },
                                               body: JSON.stringify(B.settings) });
      if (!r.ok) throw new Error(r.status);
      B.settings = (await r.json()).settings;
      $("brief-status").textContent = L("✔ gespeichert", "✔ saved");
    } catch { $("brief-status").textContent = L("Speichern fehlgeschlagen", "Saving failed"); }
  }

  function briefingFieldsChanged() {
    clearTimeout(B.timer);
    B.timer = setTimeout(() => saveBriefing({
      lookahead_days: Number($("brief-days").value) || 0,
      news_topics: $("brief-topics").value.split(",").map((t) => t.trim()).filter(Boolean),
      news_count: Number($("brief-count").value) || 3,
      inbox_tag: $("brief-inbox").value.trim(),
      instructions: $("brief-instr").value.trim(),
    }), 600);
  }
  ["brief-days", "brief-topics", "brief-count", "brief-inbox", "brief-instr"].forEach((id) => {
    $(id).addEventListener("input", briefingFieldsChanged);
  });
  $("brief-reset").onclick = async () => {
    await fetch("/api/briefing", { method: "DELETE" });
    await loadBriefing();
  };
  $("brief-preview").onclick = async () => {
    const out = $("brief-out");
    out.classList.remove("hidden");
    out.textContent = L("Briefing wird zusammengestellt …", "Putting the briefing together …");
    try {
      const r = await fetch("/api/briefing/preview", { method: "POST" });
      out.textContent = (await r.json()).text;
    } catch { out.textContent = L("Vorschau fehlgeschlagen", "Preview failed"); }
  };

  async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }

  function setPill(id, cls, text) {
    const el = $(id);
    el.className = "pill " + cls;
    el.querySelector("em").textContent = text;
  }

  async function loadStatus() {
    try {
      const st = await getJSON("/api/status");
      S.status = st;
      if (st.name) document.querySelector(".brand-name").textContent = st.name.toUpperCase();  // Persona (assistant_name)
      const l = st.llm;
      const name = l.label && l.label !== l.model ? `${l.label} · ${l.model}` : l.model;
      if (l.switching) { S.modelSwitching = S.modelSwitching || l.switching; refresh(); }
      setPill("pill-llm", l.switching ? "warn" : !l.online ? "bad" : l.model_available ? "ok" : "warn",
        l.switching ? L("lädt …", "loading …") : !l.online ? `${l.label || l.model} offline` : l.model_available ? name : l.model + L(" fehlt", " missing"));
      const v = st.voice;
      const vCls = v.stt && v.tts ? "ok" : v.stt || v.tts ? "warn" : "bad";
      setPill("pill-voice", vCls, [v.stt ? "STT" : null, v.tts ? "TTS" : "TTS(Browser)", v.wake ? "WAKE" : null].filter(Boolean).join(" · "));
      setPill("pill-mem", "ok", `${st.memory.days} ${L("Tage", "days")} · ${st.memory.facts} ${L("Fakten", "facts")}`);
      return st;
    } catch {
      setPill("pill-llm", "bad", "?");
      return null;
    }
  }
  setInterval(() => { if (S.connected) loadStatus(); }, 20000);

  // ---------------------------------------------------------------- Chat-Historie
  async function api(method, url, body) {
    const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {},
                                 body: body ? JSON.stringify(body) : undefined });
    if (!r.ok) {
      let msg = `${L("Fehler", "Error")} ${r.status}`;
      try { msg = (await r.json()).detail || msg; } catch { /* egal */ }
      toast(msg);
      throw new Error(msg);
    }
    return r.json();
  }

  function chatWhen(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    const today = new Date();
    const time = d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === today.toDateString()) return `${L("heute", "today")} ${time}`;
    if (d.toDateString() === new Date(Date.now() - 86400000).toDateString()) return `${L("gestern", "yesterday")} ${time}`;
    return d.toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit", year: "2-digit" }) + " " + time;
  }

  let chatSearchTimer = null;
  async function loadChats() {
    const q = $("chat-search").value.trim();
    let list;
    try { list = await getJSON("/api/chats" + (q ? "?q=" + encodeURIComponent(q) : "")); } catch { return; }
    const ul = $("chat-list");
    ul.innerHTML = "";
    if (!list.length) {
      ul.innerHTML = `<li class="empty">${q ? L("Nichts gefunden.", "Nothing found.") : L("Noch keine Chats.", "No chats yet.")}</li>`;
      return;
    }
    for (const c of list) {
      const li = document.createElement("li");
      li.className = "chat-item" + (c.active ? " active" : "");
      li.innerHTML = `<button class="star${c.starred ? " on" : ""}" title="${c.starred ? L("Markierung entfernen", "Remove mark") : L("Als wichtig markieren", "Mark as important")}">${c.starred ? "★" : "☆"}</button>
        <div><div class="t"></div><div class="m"></div><div class="p"></div></div>
        <button class="del" title="${L("Chat löschen (auch aus dem Gedächtnis)", "Delete chat (also from memory)")}">✕</button>`;
      li.querySelector(".t").textContent = c.title;
      li.querySelector(".m").textContent = `${chatWhen(c.updated)} · ${c.messages} ${L("Nachr.", "msgs")}` + (c.active ? L(" · AKTIV", " · ACTIVE") : "");
      li.querySelector(".p").textContent = c.preview || "";
      li.onclick = () => { if (!c.active) api("POST", `/api/chats/${c.id}/activate`).catch(() => {}); };
      li.querySelector(".t").ondblclick = (e) => {
        e.stopPropagation();
        const title = prompt(L("Neuer Titel:", "New title:"), c.title);
        if (title && title.trim()) api("PATCH", `/api/chats/${c.id}`, { title }).then(loadChats).catch(() => {});
      };
      li.querySelector(".star").onclick = (e) => {
        e.stopPropagation();
        api("POST", `/api/chats/${c.id}/star`, { starred: !c.starred }).then(loadChats).catch(() => {});
      };
      li.querySelector(".del").onclick = (e) => {
        e.stopPropagation();
        const extra = c.legacy ? L("\n\nHinweis: Dieser Chat stammt von vor der Chat-Historie – ältere Tagebuch-Einträge "
          + "daraus lassen sich nicht zuordnen und bleiben im Gedächtnis.",
          "\n\nNote: this chat predates the chat history – older journal entries from it cannot be attributed and stay in memory.") : "";
        if (confirm(L(`„${c.title}“ löschen?\n\nDer Chat wird auch aus Jarvis' Gedächtnis entfernt (Tagebuch, Suche, `
            + `Tageszusammenfassung). Gelernte Fakten bleiben.`, `Delete “${c.title}”?\n\nThe chat is also removed from `
            + `Jarvis' memory (journal, search, daily summary). Learned facts are kept.`) + extra)) {
          api("DELETE", `/api/chats/${c.id}`).then(() => { toast(L("Chat gelöscht.", "Chat deleted.")); loadChats(); }).catch(() => {});
        }
      };
      ul.appendChild(li);
    }
  }
  $("chat-search").addEventListener("input", () => {
    clearTimeout(chatSearchTimer);
    chatSearchTimer = setTimeout(loadChats, 250);
  });
  $("chat-new").onclick = () => newChat();

  function newChat() {
    api("POST", "/api/chats").catch(() => {});
  }

  function openChatView(ev) {
    for (const id of Object.keys(assistants)) delete assistants[id];
    $("chat").innerHTML = "";
    $("chat-title").textContent = ev.title ? "· " + ev.title : "";
    loadHistory().then(() => {
      if (!$("chat").children.length) addSystem(L("Neuer Chat – frühere Chats findest du unter VERLAUF.", "New chat – earlier chats are under HISTORY."));
    });
    if (!$("tab-chats").classList.contains("hidden")) loadChats();
  }

  async function loadHistory() {
    try {
      const h = await getJSON("/api/history");
      S.historyLoaded = true;
      $("chat-title").textContent = h.chat && h.chat.title ? "· " + h.chat.title : "";
      if (h.summary) addSystem(L("Frühere Gesprächsteile sind im Gedächtnis zusammengefasst.", "Earlier parts of this chat are summarised in memory."));
      for (const m of h.messages) {
        if (m.role === "user") addUser(m.content);
        else addMsg("assistant", "JARVIS", renderMarkdown(m.content));
      }
    } catch { /* egal */ }
  }

  // ---------------------------------------------------------------- Erinnerungen
  function reminderWhen(iso) {
    const d = new Date(iso);
    const today = new Date();
    const tomorrow = new Date(Date.now() + 86400000);
    const time = d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === today.toDateString()) return `${L("heute", "today")} ${time}`;
    if (d.toDateString() === tomorrow.toDateString()) return `${L("morgen", "tomorrow")} ${time}`;
    return d.toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit" }) + " " + time;
  }

  async function loadReminders() {
    const list = $("reminders");
    try {
      const items = await getJSON("/api/reminders");
      list.innerHTML = items.length ? "" : `<li class="empty">${L("Keine anstehenden Erinnerungen.", "No upcoming reminders.")}</li>`;
      for (const r of items) {
        const li = document.createElement("li");
        li.innerHTML = `<span class="rem-when"></span><span class="rem-text"></span><button class="ghost small" title="${L("Löschen", "Delete")}">✕</button>`;
        li.querySelector(".rem-when").textContent = (r.kind === "timer" ? "⏱ " : "") + reminderWhen(r.due);
        li.querySelector(".rem-text").textContent = r.text;
        li.querySelector("button").onclick = async () => {
          await fetch(`/api/reminders/${encodeURIComponent(r.id)}`, { method: "DELETE" });
          loadReminders();
        };
        list.appendChild(li);
      }
    } catch {
      list.innerHTML = `<li class="empty">${L("Erinnerungen nicht ladbar.", "Could not load reminders.")}</li>`;
    }
  }

  function chime() {
    if (!A.ctx) return;
    const t0 = A.ctx.currentTime;
    [880, 1318.5, 1760].forEach((freq, i) => {
      const osc = A.ctx.createOscillator();
      const gain = A.ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      const t = t0 + i * 0.18;
      gain.gain.setValueAtTime(0, t);
      gain.gain.linearRampToValueAtTime(0.25, t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, t + 0.9);
      osc.connect(gain).connect(A.ctx.destination);
      osc.start(t);
      osc.stop(t + 1);
    });
  }

  function showReminder(ev) {
    $("reminder-kind").textContent = ev.late ? L("VERPASSTE ERINNERUNG", "MISSED REMINDER")
      : ev.kind === "timer" ? L("TIMER ABGELAUFEN", "TIMER FINISHED") : L("ERINNERUNG", "REMINDER");
    $("reminder-text").textContent = ev.text;
    $("reminder-banner").classList.remove("hidden");
    chime();
    addSystem("🔔 " + ev.spoken);
    if (!$("tab-memory").classList.contains("hidden")) loadReminders();
  }
  $("reminder-ok").onclick = () => $("reminder-banner").classList.add("hidden");

  async function loadMemory() {
    loadReminders();
    try {
      const [facts, days] = await Promise.all([getJSON("/api/memory/facts"), getJSON("/api/memory/days")]);
      $("facts").innerHTML = facts.length
        ? facts.map((f) => `<li>${escapeHtml(f.fact)}<small>${f.day}</small></li>`).join("")
        : `<li class="empty">${L("Noch keine Fakten gespeichert.", "No facts stored yet.")}</li>`;
      $("days").innerHTML = days.length
        ? days.map((d) => `<li><button data-day="${d.day}">${formatDay(d.day)}<span>${d.summary ? L("ZUSAMMENFASSUNG", "SUMMARY") : L("PROTOKOLL", "LOG")}</span></button></li>`).join("")
        : `<li class="empty">${L("Noch keine Einträge.", "No entries yet.")}</li>`;
      $("days").querySelectorAll("button").forEach((b) => (b.onclick = () => openDay(b.dataset.day)));
    } catch (err) {
      toast(L("Gedächtnis nicht ladbar: ", "Could not load memory: ") + err.message);
    }
  }

  function formatDay(day) {
    const d = new Date(day + "T12:00:00");
    return d.toLocaleDateString(LOCALE, { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" });
  }

  async function openDay(day) {
    const d = await getJSON("/api/memory/day/" + day);
    $("day-title").textContent = formatDay(day).toUpperCase();
    let html = "";
    if (d.summary) html += `<h3>${L("ZUSAMMENFASSUNG", "SUMMARY")}</h3><div class="body">${renderMarkdown(d.summary.replace(/^# .*\n/, ""))}</div>`;
    if (d.journal) html += `<h3>${L("PROTOKOLL", "LOG")}</h3><pre>${escapeHtml(d.journal.replace(/^# .*\n/, ""))}</pre>`;
    $("day-body").innerHTML = html || L("Keine Einträge.", "No entries.");
    $("day-modal").classList.remove("hidden");
  }
  $("day-close").onclick = () => $("day-modal").classList.add("hidden");

  // ---------------------------------------------------------------- Uhr
  function tick() {
    const now = new Date();
    $("clock-time").textContent = now.toLocaleTimeString(LOCALE);
    $("clock-date").textContent = now.toLocaleDateString(LOCALE, { weekday: "long", day: "2-digit", month: "long", year: "numeric" }).toUpperCase();
  }
  setInterval(tick, 1000);
  tick();

  // ---------------------------------------------------------------- Boot
  const bootLines = $("boot-lines");
  function bootLine(text, cls) {
    const el = document.createElement("div");
    el.className = cls || "";
    el.textContent = text;
    bootLines.appendChild(el);
  }

  async function bootSequence() {
    const steps = EN ? ["> Initialising neural interface …", "> Loading memory matrix …", "> Checking subsystems …"]
      : ["> Initialisiere neuronale Schnittstelle …", "> Lade Gedächtnismatrix …", "> Prüfe Subsysteme …"];
    for (const s of steps) { bootLine(s); await new Promise((r) => setTimeout(r, 220)); }
    const st = await loadStatus();
    if (!st) { bootLine(L("  ✘ Server nicht erreichbar", "  ✘ Server unreachable"), "bad"); return; }
    const l = st.llm;
    bootLine(`  ${l.online && l.model_available ? "✔" : "✘"} ${L("Sprachmodell", "Language model")} ${l.model}${l.online ? (l.model_available ? "" : L(" (nicht geladen – ollama pull)", " (not loaded – ollama pull)")) : " (Server offline)"}`,
      l.online && l.model_available ? "ok" : "bad");
    bootLine(`  ${st.voice.stt ? "✔" : "✘"} ${L("Spracherkennung", "Speech recognition")}`, st.voice.stt ? "ok" : "bad");
    bootLine(`  ${st.voice.tts ? L("✔ Sprachausgabe (Piper)", "✔ Speech output (Piper)") : L("~ Sprachausgabe über Browser", "~ Speech output via browser")}`, st.voice.tts ? "ok" : "bad");
    bootLine(`  ${st.voice.wake ? "✔" : "✘"} Wake-Word ${L("„Hey Jarvis“", "“Hey Jarvis”")}`, st.voice.wake ? "ok" : "bad");
    if (st.trilium && st.trilium.enabled) {
      bootLine(`  ${st.trilium.online ? L("✔ Trilium verbunden (v", "✔ Trilium connected (v") + st.trilium.version + ")" : "✘ Trilium: " + (st.trilium.error || "offline")}`,
        st.trilium.online ? "ok" : "bad");
    }
    bootLine(L(`  ✔ Gedächtnis: ${st.memory.days} Tage, ${st.memory.facts} Fakten, ${st.memory.chunks} Einträge`,
               `  ✔ Memory: ${st.memory.days} days, ${st.memory.facts} facts, ${st.memory.chunks} entries`), "ok");
  }

  $("boot-btn").onclick = async () => {
    await initAudio().catch(() => {});
    $("boot").classList.add("hidden");
    if (S.wake) {
      if (await initMic()) send({ type: "wake", enabled: true });
      else { S.wake = false; store.set("wake", false); }
      updateMicStreaming();
    }
    $("input").focus();
    refresh();
  };

  bootSequence();
  connect();
  refresh();
})();
