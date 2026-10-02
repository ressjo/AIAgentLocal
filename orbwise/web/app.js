/* Orbwise – Web-Client: WebSocket, Chat, Tool-Aktivität, Bestätigungen, Mikrofon und Sprachausgabe. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const orb = new window.Orb($("orb"), $("orb-overlay"));
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
    ? { running: "running", waiting: "waiting", ok: "done", denied: "denied", blocked: "blocked", error: "error",
        planned: "planned" }
    : { running: "läuft", waiting: "wartet", ok: "fertig", denied: "abgelehnt", blocked: "blockiert", error: "fehler",
        planned: "geplant" };

  const store = {
    get(k, d) { try { const v = localStorage.getItem("orbwise." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("orbwise." + k, JSON.stringify(v)); } catch { /* egal */ } },
  };

  const S = {
    ws: null, connected: false, retry: 0,
    serverState: "idle", substate: "",
    tts: store.get("tts", true), wake: store.get("wake", false), think: store.get("think", false), auto: store.get("auto", true), plan: store.get("plan", false),
    voiceName: store.get("voice", ""), fxOn: store.get("fx", true), fxAmount: store.get("fxAmount", 0.6),
    recording: false, transcribing: false, streamMic: false,
    playing: false, confirm: null, confirmListenSent: false,
    historyLoaded: false, status: null, errorUntil: 0, modelDoneAt: 0, startupModel: null,
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
    // ohne vorherigen Klick bleibt der Kontext pausiert, bis der Nutzer etwas anklickt (nicht darauf warten)
    if (A.ctx.state === "suspended") A.ctx.resume().catch(() => {});
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
    // nicht bei bearbeitbaren Fenstern (Mail): dort wird per Klick gesendet
    if (S.confirm && !(S.confirm.editable || []).length && !S.confirmListenSent && A.micReady && (S.wake || S.voiceUsed)) {
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
    const loadingModel = S.modelSwitching || S.startupModel;
    if (loadingModel && S.connected) s = "thinking";
    orb.setState(s);
    const label = $("state-label");
    label.textContent = loadingModel && S.connected ? L("LADE MODELL", "LOADING MODEL") : (LABELS[s] || s.toUpperCase());
    label.style.color = { listening: "#3ddc97", executing: "#f5b14c", confirm: "#f5b14c", error: "#ff5f6d", offline: "#8a92a5", thinking: "#a9b3ff" }[s] || "";
    let sub = S.substate || (S.startupModel && !S.modelSwitching ? S.startupModel : "");
    if (S.recording) sub = S.recordingMode === "ptt" ? L("Loslassen zum Senden", "Release to send") : L("Sprich jetzt …", "Speak now …");
    else if (S.transcribing) sub = L("Transkribiere …", "Transcribing …");
    else if (s === "idle" && S.wake) sub = L("Sag „Hey Jarvis“", "Say “Hey Jarvis”");
    $("substate-label").textContent = sub || "";
    $("btn-mic").classList.toggle("recording", S.recording);
    $("btn-wake").classList.toggle("on", S.wake);
    $("btn-tts").classList.toggle("on", S.tts);
    $("btn-think").classList.toggle("on", S.think);
    $("btn-auto").classList.toggle("on", S.auto);
    $("btn-plan").classList.toggle("on", S.plan);
    // Senden wird während einer Anfrage zum Stopp-Knopf
    const busy = S.connected && (["thinking", "executing", "confirm", "speaking"].includes(s) || S.playing);
    $("btn-send").classList.toggle("hidden", busy);
    $("btn-stop").classList.toggle("hidden", !busy);
    // Schalter in den Einstellungen spiegeln die Knöpfe in Kopf- und Eingabezeile
    document.querySelectorAll("[data-mirror]").forEach((sw) => {
      const on = $(sw.dataset.mirror).classList.contains("on");
      sw.classList.toggle("on", on);
      sw.textContent = on ? L("An", "On") : L("Aus", "Off");
    });
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
      send({ type: "auto_read", enabled: S.auto });
      send({ type: "plan_mode", enabled: S.plan });
      sendVoiceSettings();
      if (S.wake && A.micReady) send({ type: "wake", enabled: true });
      refresh();
      loadStatus();
      getJSON("/api/metrics").then(showMetrics).catch(() => {});
      if (!S.historyLoaded) loadHistory();
      renderBoot();
    };
    ws.onclose = () => {
      S.connected = false;
      S.recording = false;
      S.transcribing = false;
      updateMicStreaming();
      refresh();
      // auf der Startseite zügig neu versuchen (der Server startet evtl. gerade), danach mit Abstand
      const delay = Boot.entered ? Math.min(10000, 800 * 2 ** S.retry++) : 1000;
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
      case "startup":
        Boot.server = ev;
        renderBoot();
        return;
      case "hello":
        S.serverState = ev.busy ? "thinking" : "idle";  // tatsächlichen Zustand übernehmen (auch nach Neuverbinden)
        S.transcribing = false;  // neue Verbindung = neue Audio-Sitzung, eine alte Transkription meldet sich nie mehr
        if (ev.context) showContext(ev.context);
        break;
      case "context":
        showContext(ev);
        return;
      case "state":
        if (ev.routine) orb.addSatellite("rt:" + ev.routine, "⟳ " + ev.routine.toUpperCase());
        if (ev.state === "idle") {  // abgebrochene Werkzeuge nicht hängen lassen
          orb.clearSatellites("rt:");
        }
        S.serverState = ev.state === "confirm" ? S.serverState : ev.state;
        S.substate = ev.state === "executing" && ev.tool ? ev.tool : "";
        break;
      case "user":
        addUser(ev.text, ev.source);
        if (ev.source === "voice") S.voiceUsed = true;
        break;
      case "assistant_start":
        startAssistant(ev.id, ev.plan);
        if (ev.plan) S.substate = L("plant …", "planning …");
        break;
      case "plan":
        showPlan(ev);
        break;
      case "plan_closed":
        closePlan(ev.id, ev.outcome);
        break;
      case "plan_mode":  // Server schaltet den Planmodus aus (z. B. nach „Ausführen“)
        S.plan = !!ev.enabled;
        store.set("plan", S.plan);
        if (!S.plan) toast(L("Plan angenommen – Planmodus ist jetzt aus.", "Plan accepted – plan mode is now off."));
        break;
      case "token":
        appendToken(ev.id, ev.text);
        orb.token();
        T.tokenTimes.push(performance.now());
        if (Thought.active) Thought.zoomOut();
        if (S.substate === THINKING_SUB) { S.substate = ""; break; }
        return;  // Zustand ändert sich pro Token nicht – kein refresh() nötig
      case "reasoning":
        // Denkkette: nicht in die Antwort, sondern in den Orb (hineinzoomen) und später aufklappbar im Chat
        Thought.add(ev.id, ev.text || "");
        orb.token();  // Denk-Puls folgt auch dem Gedankengang
        if (S.substate !== THINKING_SUB) S.substate = THINKING_SUB;
        else return;
        break;
      case "segment_end":
        appendToken(ev.id, "\n\n");
        break;
      case "assistant_end":
        Thought.finish(ev.id);
        finishAssistant(ev.id, ev.cancelled);
        S.serverState = "idle";  // Antwort fertig = bereit, auch wenn das „idle“ des Servers noch aussteht
        if (S.substate === THINKING_SUB) S.substate = "";
        setTimeout(loadStatus, 300);
        if (!$("chat-title").textContent) {
          getJSON("/api/chats").then((l) => {
            const c = l.find((x) => x.active);
            if (c && c.messages) $("chat-title").textContent = c.title;
          }).catch(() => {});
        }
        if (!$("tab-chats").classList.contains("hidden")) loadChats();
        if (!$("tab-memory").classList.contains("hidden")) loadReminders();
        break;
      case "tool_call":
        toolCall(ev);  // im Denkmodus bleibt der Zoom – das Werkzeug erscheint als Chip im Gedankenkasten
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
        if (S.modelSwitching && Boot.entered && !Boot.startupLoad) addSystem(L("Modell aktiv: ", "Model active: ") + S.modelSwitching);
        S.modelSwitching = null;
        S.modelDoneAt = Date.now();
        S.substate = "";
        loadStatus();
        break;
      case "models_changed":
        if (!modelMenu.classList.contains("hidden") && !modelMenu.querySelector(".fit-ok, .fit-tight, .fit-big")) openModelMenu();
        loadStatus();
        return;
      case "model_pull":
        modelPullEvent(ev);
        return;
      case "model_error":
        S.modelSwitching = null;
        S.modelDoneAt = Date.now();
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
      case "routines_changed":
        if (!$("tab-planner").classList.contains("hidden")) loadRoutines();
        return;
      case "routine_done":
        orb.removeSatellite("rt:" + ev.name, ev.status === "error");
        toast(ev.status === "error" ? L(`Routine „${ev.name}“ fehlgeschlagen`, `Routine “${ev.name}” failed`)
          : L(`Routine „${ev.name}“ erledigt – im VERLAUF`, `Routine “${ev.name}” done – see HISTORY`));
        if (!$("tab-planner").classList.contains("hidden")) loadRoutines();
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
      blocks.push(`<div class="code"><button class="copy" type="button" title="${L("Kopieren", "Copy")}">⧉</button>`
        + `<pre>${escapeHtml(code.replace(/\n$/, ""))}</pre></div>`);
      return `\u0000${blocks.length - 1}\u0000`;
    });
    text = escapeHtml(text)
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
    const html = text.split(/\n{2,}/).map((para) => {
      if (/^\u0000\d+\u0000$/.test(para.trim())) return para.trim();
      // Zeilenweise: Überschriften (## …), Listen (- … / 1. …) und normaler Text, auch gemischt in einem Absatz
      const out = [];
      let list = null, text = [];
      const flushText = () => { if (text.length) out.push(`<p>${text.join("<br>")}</p>`); text = []; };
      const flushList = () => { if (list) out.push(`<${list.tag}>${list.items.join("")}</${list.tag}>`); list = null; };
      for (const l of para.split("\n")) {
        const h = /^\s*#{1,4}\s+(.+)$/.exec(l);
        const li = /^\s*([-*•]|\d+[.)])\s+(.*)$/.exec(l);
        if (h) { flushText(); flushList(); out.push(`<div class="md-h">${h[1]}</div>`); }
        else if (li) {
          const tag = /\d/.test(li[1]) ? "ol" : "ul";
          flushText();
          if (list && list.tag !== tag) flushList();
          list = list || { tag, items: [] };
          list.items.push(`<li>${li[2]}</li>`);
        } else if (l.trim()) { flushList(); text.push(l); }
      }
      flushText(); flushList();
      return out.join("");
    }).join("");
    return html.replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
  }

  function scrollChat() { chat.scrollTop = chat.scrollHeight; }

  // Kopieren-Knopf an Code-Blöcken (Ereignis-Delegation, auch für später gerenderte Nachrichten)
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".code .copy");
    if (!btn) return;
    const text = btn.parentElement.querySelector("pre").textContent;
    try { await navigator.clipboard.writeText(text); } catch {
      const r = document.createRange(); r.selectNodeContents(btn.parentElement.querySelector("pre"));
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); document.execCommand("copy"); sel.removeAllRanges();
    }
    btn.textContent = "✔"; btn.classList.add("done");
    setTimeout(() => { btn.textContent = "⧉"; btn.classList.remove("done"); }, 1200);
  });

  // Ecken eines Panels kurz aufleuchten lassen (neue Nachricht / neue Aktivität)
  function flashPanel(el) {
    const panel = el && el.closest(".panel");
    if (!panel) return;
    panel.classList.remove("flash");
    void panel.offsetWidth;  // Animation neu starten
    panel.classList.add("flash");
  }

  function addMsg(cls, who, html) {
    const el = document.createElement("div");
    el.className = "msg " + cls;
    el.innerHTML = `<div class="who">${who}</div><div class="tools"></div><div class="body">${html}</div>`;
    chat.appendChild(el);
    flashPanel(chat);
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

  function startAssistant(id, plan) {
    const el = addMsg("assistant streaming" + (plan ? " plan" : ""), "JARVIS", "");
    el.dataset.msg = id;
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
        $("stage").classList.add("zoomed");
        if (!isCompact()) orb.setZoom(true);  // Satelliten kreisen dann um den Gedankenkasten
        else requestAnimationFrame(scrollChat);  // Chat rückt über die Gedanken-Karte
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
      this.outTimer = setTimeout(() => { $("stage").classList.remove("zoomed"); orb.setZoom(false); }, wait);
    },
    finish(id) {
      this.zoomOut();
      const text = (this.byMsg[id] || "").trim();
      delete this.byMsg[id];
      const a = assistants[id];
      if (text && a) {
        const d = document.createElement("details");
        d.className = "thought-log";
        d.innerHTML = `<summary>${L("Gedankengang", "Thoughts")}</summary><pre></pre>`;
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

  // ---------------------------------------------------------------- Planmodus
  const PLAN_OUTCOME = {
    accepted: L("▶ wird ausgeführt", "▶ being carried out"), revised: L("✎ wird überarbeitet", "✎ being revised"),
    discarded: L("✕ verworfen", "✕ discarded"), replaced: L("ersetzt", "replaced"),
  };
  function showPlan(ev) {
    const el = chat.querySelector(`.msg[data-msg="${ev.id}"]`);
    if (!el || el.querySelector(".plan-bar")) return;
    const bar = document.createElement("div");
    bar.className = "plan-bar";
    bar.innerHTML = `<div class="plan-actions">
        <button class="btn plan-run">▶ ${L("AUSFÜHREN", "RUN")}</button>
        <button class="btn plan-edit">✎ ${L("ÄNDERN", "CHANGE")}</button>
        <button class="btn plan-drop">✕ ${L("VERWERFEN", "DISCARD")}</button></div>
      <form class="plan-revise hidden"><input type="text" maxlength="4000"
        placeholder="${L("Was soll anders sein? (Enter sendet)", "What should be different? (Enter sends)")}"></form>
      <div class="plan-status"></div>`;
    el.appendChild(bar);
    const input = bar.querySelector("input");
    bar.querySelector(".plan-run").onclick = () => send({ type: "plan_accept", id: ev.id });
    bar.querySelector(".plan-drop").onclick = () => send({ type: "plan_discard", id: ev.id });
    bar.querySelector(".plan-edit").onclick = () => {
      bar.querySelector(".plan-revise").classList.toggle("hidden");
      input.focus();
    };
    bar.querySelector(".plan-revise").onsubmit = (e) => {
      e.preventDefault();
      if (input.value.trim()) send({ type: "plan_revise", id: ev.id, text: input.value.trim() });
    };
    scrollChat();
  }
  function closePlan(id, outcome) {
    const bar = chat.querySelector(`.msg[data-msg="${id}"] .plan-bar`);
    if (!bar) return;
    bar.querySelectorAll("button, input").forEach((b) => { b.disabled = true; });
    bar.querySelector(".plan-revise").classList.add("hidden");
    bar.querySelector(".plan-status").textContent = PLAN_OUTCOME[outcome] || outcome;
    bar.classList.add("closed", outcome);
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

  // Kurzname für den Werkzeug-Satelliten am Orb
  const SAT_GROUPS = [
    [/^look_at_/, ["SEHEN", "VISION"]], [/^(web_search|fetch_url|open_website)$/, ["WEB", "WEB"]], [/^paperless_/, ["PAPERLESS", "PAPERLESS"]],
    [/^mail_/, ["MAIL", "MAIL"]], [/^obsidian_/, ["OBSIDIAN", "OBSIDIAN"]], [/^trilium_/, ["TRILIUM", "TRILIUM"]],
    [/^ha_/, ["SMART HOME", "SMART HOME"]], [/^calendar_/, ["KALENDER", "CALENDAR"]], [/^run_shell$/, ["SHELL", "SHELL"]],
    [/(package|system_update)/, ["PAKETE", "PACKAGES"]], [/(file|folder)/, ["DATEIEN", "FILES"]],
    [/^weather/, ["WETTER", "WEATHER"]], [/^routine_/, ["ROUTINEN", "ROUTINES"]], [/reminder/, ["ERINNERUNG", "REMINDER"]],
    [/(remember|recall|forget|memory)/, ["GEDÄCHTNIS", "MEMORY"]],
  ];
  function satLabel(name) {
    const hit = SAT_GROUPS.find(([rx]) => rx.test(name));
    return hit ? hit[1][EN ? 1 : 0] : name.split("_")[0].toUpperCase();
  }

  // Symbol je Aktionsart (weitere Symbole: ICONS in orb.js); Gedächtnis leuchtet im Kern statt als Satellit
  const ICON_GROUPS = { shell: "cli", packages: "cli", sysadmin: "cli", system: "cli", power: "cli", web: "cloud",
                        mail: "mail", paperless: "paperless", vision: "eye" };

  function toolCall(ev) {
    activityArrived();
    if (ev.group === "memory_tools") orb.memoryGlow(ev.id, true, satLabel(ev.name));
    else orb.addSatellite(ev.id, satLabel(ev.name), ICON_GROUPS[ev.group] || null);
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
    if (ev.routine) el.querySelector(".act-name").textContent = `⟳ ${ev.routine} · ${ev.name}`;
    el.querySelector(".toggle-out").onclick = () => el.classList.toggle("open");
    activity.prepend(el);
    flashPanel(activity);
    acts[ev.id] = el;
    while (activity.children.length > 60) activity.lastChild.remove();

    const a = !ev.routine && S.currentMsg && assistants[S.currentMsg];
    if (a) {  // über der Nachricht nur der aktuelle Aufruf – die früheren stehen in der Aktivität
      const tools = a.el.querySelector(".tools");
      a.toolCount = (a.toolCount || 0) + 1;
      tools.replaceChildren();
      const chip = document.createElement("span");
      chip.className = "tool-chip running";
      chip.id = "chip-" + ev.id;
      chip.textContent = "⚙ " + ev.name;
      tools.appendChild(chip);
      if (a.toolCount > 1) {
        const more = document.createElement("button");
        more.type = "button";
        more.className = "tool-more";
        more.textContent = L(`+${a.toolCount - 1} vorher`, `+${a.toolCount - 1} earlier`);
        more.title = L("Alle Werkzeug-Aufrufe in der Aktivität zeigen", "Show all tool calls in the activity panel");
        more.onclick = () => setDrawer(true, true);
        tools.appendChild(more);
      }
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
    orb.removeSatellite(ev.id, ev.status === "error" || ev.status === "blocked");
    orb.memoryGlow(ev.id, false);
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
  // Felder, die der Nutzer vor dem Bestätigen noch ändern darf (z. B. mail_send)
  const EDIT_FIELDS = {
    to: [L("AN", "TO"), "input"], cc: [L("CC", "CC"), "input"],
    subject: [L("BETREFF", "SUBJECT"), "input"], body: [L("TEXT", "TEXT"), "textarea"],
  };
  const APPROVE_HTML = $("confirm-yes").innerHTML;

  function openConfirm(ev) {
    S.confirm = ev;
    S.confirmListenSent = false;
    const editable = (ev.editable || []).filter((k) => EDIT_FIELDS[k]);
    const form = $("confirm-form");
    form.innerHTML = "";
    for (const key of editable) {
      const [label, kind] = EDIT_FIELDS[key];
      const row = document.createElement("label");
      row.innerHTML = `<span>${label}</span>`;
      const field = document.createElement(kind);
      if (kind === "input") field.type = "text";
      field.dataset.key = key;
      field.value = ev.args[key] ?? "";
      field.spellcheck = key === "body" || key === "subject";
      row.appendChild(field);
      form.appendChild(row);
    }
    form.classList.toggle("hidden", !editable.length);
    $("confirm-cmd").classList.toggle("hidden", !!editable.length);
    document.querySelector(".confirm-modal").classList.toggle("editing", !!editable.length);
    $("confirm-yes").innerHTML = editable.length && ev.name === "mail_send"
      ? `${L("SENDEN", "SEND")} <kbd>Strg+Enter</kbd>` : editable.length ? `${L("AUSFÜHREN", "RUN")} <kbd>Strg+Enter</kbd>` : APPROVE_HTML;
    $("confirm-summary").textContent = ev.name === "mail_send"
      ? L("Mail prüfen, bei Bedarf ändern und senden:", "Check the e-mail, edit it if needed and send it:")
      : L(`Soll ich ${ev.summary} ausführen?`, `Shall I run ${ev.summary}?`);
    $("confirm-cmd").textContent = ev.name === "run_shell" ? ev.args.command : `${ev.name}(${JSON.stringify(ev.args, null, 2)})`;
    $("confirm-reason").textContent = ev.reason && !editable.length ? L("Grund: ", "Reason: ") + ev.reason : "";
    $("confirm-voice").textContent = editable.length ? L("Senden nur per Klick – „Nein“ bricht ab.", "Send only by click – “no” cancels.")
      : A.micReady ? L("oder sag „Ja“ bzw. „Nein“", "or say “yes” or “no”") : "";
    $("confirm-voice").classList.remove("listening");
    $("confirm").classList.remove("hidden");
    const act = acts[ev.id];
    if (act) act.querySelector(".act-status").textContent = STATUS_TEXT.waiting;
    setTimeout(() => (form.querySelector("input, textarea") || $("confirm-yes")).focus(), 50);
    refresh();
    if (!S.tts) onSpeechDrained();
  }

  function editedFields() {
    const out = {};
    for (const f of $("confirm-form").querySelectorAll("[data-key]")) out[f.dataset.key] = f.value;
    return out;
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
    const editing = !$("confirm-form").classList.contains("hidden");
    const args = editing && approved ? editedFields() : null;
    if (args && "to" in args && !args.to.trim()) {
      const to = $("confirm-form").querySelector('[data-key="to"]');
      to.classList.add("invalid");
      to.focus();
      toast(L("Bitte einen Empfänger eintragen.", "Please enter a recipient."));
      return;
    }
    send(args ? { type: "confirm", id: S.confirm.id, approved, args } : { type: "confirm", id: S.confirm.id, approved });
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

  // Leertaste = Push-to-talk – aber nie, während man tippt (Chat, Planer, Briefing …) oder ein Knopf den Fokus hat
  function typingTarget(el) {
    return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(el.tagName));
  }

  document.addEventListener("keydown", (e) => {
    if (!$("boot").classList.contains("hidden")) return;
    if (pwId) return;  // Passwortfeld hat Vorrang (kein Push-to-talk mit Leertaste)
    if (S.confirm) {
      // im bearbeitbaren Fenster: Enter schreibt (neue Zeile), Strg/Cmd+Enter sendet
      const inForm = $("confirm-form").contains(document.activeElement);
      if (e.key === "Enter" && inForm && !(e.ctrlKey || e.metaKey)) return;
      if (e.key === "Enter") { e.preventDefault(); answerConfirm(true); }
      if (e.key === "Escape") { e.preventDefault(); answerConfirm(false); }
      return;
    }
    if (e.key === "Escape") {
      if (!$("day-modal").classList.contains("hidden")) { $("day-modal").classList.add("hidden"); return; }
      if (closeSheet()) return;
      if (app.classList.contains("side-open")) { app.classList.remove("side-open"); $("scrim").classList.add("hidden"); return; }
      send({ type: "stop" });
      stopSpeech(true);
      return;
    }
    if (e.code === "Space" && !e.repeat && !typingTarget(document.activeElement)) {
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

  $("btn-plan").onclick = () => {
    S.plan = !S.plan;
    store.set("plan", S.plan);
    send({ type: "plan_mode", enabled: S.plan });
    toast(S.plan ? L("Planmodus an – Jarvis legt erst einen Plan vor, ausgeführt wird nach deiner Freigabe.",
                     "Plan mode on – Jarvis presents a plan first and carries it out once you approve it.")
                 : L("Planmodus aus – Jarvis legt direkt los.", "Plan mode off – Jarvis gets going right away."));
    refresh();
  };

  $("btn-think").onclick = () => {
    S.think = !S.think;
    store.set("think", S.think);
    send({ type: "think", enabled: S.think });
    toast(S.think ? L("Denkmodus an – Antworten dauern länger, der Gedankengang erscheint beim Orb.",
                      "Thinking mode on – answers take longer, the reasoning appears next to the orb.")
                  : L("Denkmodus aus – schnelle Antworten.", "Thinking mode off – fast answers."));
    refresh();
  };

  $("btn-auto").onclick = () => {
    S.auto = !S.auto;
    store.set("auto", S.auto);
    send({ type: "auto_read", enabled: S.auto });
    toast(S.auto ? L("Auto an – erkannte lesende Befehle laufen ohne Rückfrage, Veränderndes fragt weiter.",
                     "Auto on – recognised read-only commands run without asking, changes still ask.")
                 : L("Auto aus – jeder Shell-Befehl fragt vorher.", "Auto off – every shell command asks first."));
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

  // ---------------------------------------------------------------- Navigation: Sheets, Seitenleiste, Aktivität
  const app = $("app");
  const main = $("main");
  function isCompact() { return main.classList.contains("has-messages"); }
  // Kompakter Orb, sobald das Gespräch Nachrichten hat (Begrüßung + großer Orb nur im leeren Chat)
  const updateStage = () => {
    const has = !!$("chat").querySelector(".msg.user, .msg.assistant");
    if (has !== isCompact()) {
      main.classList.toggle("has-messages", has);
      if (has) orb.setZoom(false);
    }
  };
  new MutationObserver(updateStage).observe($("chat"), { childList: true });

  let openSheetName = null;
  function openSheet(name, section) {
    closeSheet();
    openSheetName = name;
    $("sheet-" + name).classList.remove("hidden");
    $("scrim").classList.remove("hidden");
    document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.sheet === name));
    app.classList.remove("side-open");
    if (name === "planner") loadPlanner();
    if (name === "memory") loadMemory();
    if (name === "settings") showSection(section || "models");
  }
  function closeSheet() {
    if (!openSheetName) return false;
    $("sheet-" + openSheetName).classList.add("hidden");
    $("scrim").classList.add("hidden");
    document.querySelectorAll(".nav-item").forEach((n) => n.classList.remove("active"));
    openSheetName = null;
    return true;
  }
  function showSection(section) {
    document.querySelectorAll(".set-tab").forEach((t) => t.classList.toggle("active", t.dataset.section === section));
    document.querySelectorAll(".set-section").forEach((el) => el.classList.toggle("hidden", el.id !== "set-" + section));
    if (section === "models") openModelMenu();
    if (section === "voice") openVoiceMenu();
    if (section === "status") loadStatus();
  }
  document.querySelectorAll(".nav-item").forEach((n) => { n.onclick = () => openSheet(n.dataset.sheet); });
  document.querySelectorAll(".set-tab").forEach((t) => { t.onclick = () => showSection(t.dataset.section); });
  document.querySelectorAll("[data-close-sheet]").forEach((b) => { b.onclick = closeSheet; });
  $("scrim").onclick = () => { closeSheet(); app.classList.remove("side-open"); };
  document.querySelectorAll("[data-mirror]").forEach((sw) => { sw.onclick = () => $(sw.dataset.mirror).click(); });

  // Seitenleiste ein-/ausklappen (Desktop) bzw. als Menü öffnen (schmal)
  app.classList.toggle("side-collapsed", !!store.get("sideCollapsed", false));
  $("btn-sidebar").onclick = () => {
    const c = !app.classList.contains("side-collapsed");
    app.classList.toggle("side-collapsed", c);
    store.set("sideCollapsed", c);
  };
  $("btn-menu").onclick = () => { app.classList.add("side-open"); $("scrim").classList.remove("hidden"); };

  // Aktivität: öffnet sich beim ersten Werkzeug von selbst – außer man hat sie bewusst geschlossen
  let actUnseen = 0;
  function setDrawer(open, byUser) {
    app.classList.toggle("drawer-open", open);
    if (byUser) store.set("drawer", open ? "open" : "closed");
    if (open) { actUnseen = 0; $("act-badge").classList.add("hidden"); }
    $("btn-activity").classList.toggle("on", open);
  }
  setDrawer(store.get("drawer", "") === "open" && window.innerWidth > 1200);
  $("btn-activity").onclick = () => setDrawer(!app.classList.contains("drawer-open"), true);
  $("drawer-close").onclick = () => setDrawer(false, true);
  function activityArrived() {
    if (app.classList.contains("drawer-open")) return;
    if (store.get("drawer", "") !== "closed" && window.innerWidth > 1200) { setDrawer(true); return; }
    actUnseen += 1;
    $("act-badge").textContent = actUnseen > 9 ? "9+" : String(actUnseen);
    $("act-badge").classList.remove("hidden");
  }

  // ---------------------------------------------------------------- Stimme & Effekt
  function fxRate() {
    return S.fxOn ? +(1 - 0.08 * S.fxAmount).toFixed(3) : 1;
  }

  function sendVoiceSettings() {
    send({ type: "voice_settings", voice: S.voiceName || undefined, rate: fxRate() });
  }

  function renderFx() {
    $("fx-toggle").textContent = S.fxOn ? L("An", "On") : L("Aus", "Off");
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

  // Stimmen-Menü an der VOICE-Pille: auswählen, anhören, löschen, hinzufügen (wie das Modell-Menü)
  const voiceMenu = $("voice-menu");
  async function openVoiceMenu(catalog = false) {
    closeModelMenu();
    const list = $("voice-list");
    let data;
    try { data = await getJSON("/api/voices"); } catch { toast(L("Stimmen nicht ladbar", "Could not load voices")); return; }
    const st = S.status && S.status.voice;
    list.innerHTML = `<div class="mm-title">${catalog ? L("STIMME HINZUFÜGEN", "ADD VOICE") : L("STIMME WÄHLEN", "CHOOSE VOICE")}</div>
      <div class="mm-hint"></div>`;
    list.querySelector(".mm-hint").textContent = st ? [
      st.stt ? L("Spracherkennung ✔", "Speech recognition ✔") : L("Spracherkennung aus", "Speech recognition off"),
      st.wake ? L("Wake-Word ✔", "Wake word ✔") : L("Wake-Word aus", "Wake word off")].join(" · ") : "";
    if (!data.available) {
      list.insertAdjacentHTML("beforeend", `<div class="mm-hint">${L("Piper-Sprachausgabe ist deaktiviert – es spricht der Browser.",
        "Piper speech output is disabled – the browser speaks instead.")}</div>`);
    } else if (!catalog) {
      if (!S.voiceName) S.voiceName = data.current;
      for (const v of data.voices.filter((x) => x.installed)) list.appendChild(voiceRow(v, data.current));
      const add = document.createElement("button");
      add.className = "model-item add";
      add.textContent = L("+ STIMME HINZUFÜGEN …", "+ ADD VOICE …");
      add.onclick = (e) => { e.stopPropagation(); openVoiceMenu(true); };
      list.appendChild(add);
      // eigene Stimme (z. B. von huggingface.co): .onnx + .onnx.json wählen oder aufs Menü ziehen
      const up = document.createElement("button");
      up.className = "model-item add vm-upload";
      up.innerHTML = `<div>${L("⬆ EIGENE STIMME HOCHLADEN …", "⬆ UPLOAD OWN VOICE …")}</div><div class="mi-note"></div>`;
      up.querySelector(".mi-note").textContent = L("Piper-Stimme, z. B. von huggingface.co – beide Dateien (.onnx + .onnx.json), auch per Drag & Drop",
        "Piper voice, e.g. from huggingface.co – both files (.onnx + .onnx.json), drag & drop works too");
      up.onclick = (e) => {
        e.stopPropagation();
        const input = document.createElement("input");
        input.type = "file";
        input.multiple = true;
        input.accept = ".onnx,.json";
        input.onchange = () => uploadVoice([...input.files]);
        input.click();
      };
      list.appendChild(up);
    } else {
      // ganzer Piper-Katalog: Auswahl (empfohlen) zuerst, dann alle weiteren nach Region, mit Suchfeld
      const missing = data.voices.filter((x) => !x.installed);
      const tools = document.createElement("div");
      tools.className = "vm-tools";
      tools.innerHTML = `<input type="search" class="vm-search" placeholder="${L("Stimme suchen …", "Search voices …")}">
        <a href="https://rhasspy.github.io/piper-samples/" target="_blank" rel="noopener">${L("Probehören ↗", "Listen to samples ↗")}</a>`;
      list.appendChild(tools);
      const box = document.createElement("div");
      list.appendChild(box);
      const render = (q) => {
        box.innerHTML = "";
        const hits = missing.filter((v) => !q || `${v.label} ${v.name} ${v.description}`.toLowerCase().includes(q));
        if (!hits.length) {
          box.innerHTML = `<div class="mm-hint">${missing.length ? L("Keine Treffer.", "No matches.")
            : L("Alle Stimmen sind installiert.", "All voices are installed.")}</div>`;
        }
        let group = null;
        for (const v of hits) {
          const g = v.recommended ? L("EMPFOHLEN", "RECOMMENDED") : (v.locale || "").toUpperCase();
          if (g !== group) {
            group = g;
            box.insertAdjacentHTML("beforeend", `<div class="mm-title vm-group"></div>`);
            box.lastElementChild.textContent = g;
          }
          box.appendChild(catalogItem(v));
        }
      };
      const search = tools.querySelector(".vm-search");
      search.onclick = (e) => e.stopPropagation();
      search.oninput = () => render(search.value.trim().toLowerCase());
      render("");
      const back = document.createElement("button");
      back.className = "model-item add";
      back.textContent = L("← ZURÜCK", "← BACK");
      back.onclick = (e) => { e.stopPropagation(); openVoiceMenu(); };
      list.appendChild(back);
    }
    voiceMenu.classList.toggle("catalog", catalog);
    voiceMenu.classList.remove("hidden");
  }

  function catalogItem(v) {
    const b = voiceItem(v, v.download_mb ? `~${v.download_mb} MB` : "");
    b.onclick = async (e) => {
      e.stopPropagation();
      b.disabled = true;
      b.querySelector(".mi-tag").textContent = L("LÄDT …", "LOADING …");
      try {
        await api("POST", `/api/voices/${encodeURIComponent(v.name)}/install`);
        toast(L(`✔ ${v.label} installiert`, `✔ ${v.label} installed`));
        loadStatus();
        openVoiceMenu();
      } catch { b.disabled = false; b.querySelector(".mi-tag").textContent = v.download_mb ? `~${v.download_mb} MB` : ""; }
    };
    return b;
  }

  function voiceItem(v, tag) {
    const b = document.createElement("button");
    b.className = "model-item";
    b.innerHTML = `<div class="mi-head"><span class="mi-name"></span><span class="mi-tag"></span></div><div class="mi-sub"></div>`;
    b.querySelector(".mi-name").textContent = v.label;
    b.querySelector(".mi-tag").textContent = tag;
    b.querySelector(".mi-sub").textContent = v.description + (v.size_mb ? ` · ${v.size_mb} MB` : "");
    return b;
  }

  function voiceRow(v, current) {
    const active = v.name === current;
    const b = voiceItem(v, active ? L("AKTIV", "ACTIVE") : v.male === null ? "" : v.male ? L("MÄNNLICH", "MALE") : L("WEIBLICH", "FEMALE"));
    if (active) b.classList.add("active");
    b.onclick = (e) => {
      e.stopPropagation();
      if (active) return;
      S.voiceName = v.name;
      store.set("voice", v.name);
      sendVoiceSettings();
      toast(L(`Stimme: ${v.label}`, `Voice: ${v.label}`));
      setTimeout(() => openVoiceMenu(), 150);
    };
    const row = document.createElement("div");
    row.className = "model-row";
    const play = document.createElement("button");
    play.className = "model-del voice-play";
    play.textContent = "▶";
    play.title = L("Anhören", "Preview");
    play.onclick = (e) => { e.stopPropagation(); previewVoice(v.name); };
    const del = document.createElement("button");
    del.className = "model-del";
    del.textContent = "🗑";
    del.disabled = active;
    del.title = active ? L("Aktive Stimme – erst eine andere wählen", "Active voice – choose another one first") : L("Stimme löschen", "Delete voice");
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(L(`Stimme ${v.label} löschen?`, `Delete voice ${v.label}?`))) return;
      try {
        await api("DELETE", `/api/voices/${encodeURIComponent(v.name)}`);
        toast(L(`✔ ${v.label} gelöscht`, `✔ ${v.label} deleted`));
        openVoiceMenu();
      } catch { /* Meldung kommt von api() */ }
    };
    row.append(b, play, del);
    return row;
  }

  // Upload: erst die Konfiguration, dann das Modell (roher Datenstrom, Fortschritt im Menü)
  function putFile(url, file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", url);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) return resolve(JSON.parse(xhr.responseText || "{}"));
        let msg = `${L("Fehler", "Error")} ${xhr.status}`;
        try { msg = JSON.parse(xhr.responseText).detail || msg; } catch { /* egal */ }
        reject(new Error(msg));
      };
      xhr.onerror = () => reject(new Error(L("Verbindung unterbrochen", "Connection lost")));
      xhr.send(file);
    });
  }

  async function uploadVoice(files) {
    const model = files.find((f) => f.name.toLowerCase().endsWith(".onnx"));
    const config = files.find((f) => f.name.toLowerCase().endsWith(".json"));
    if (!model || !config) {
      toast(L("Bitte beide Dateien wählen: .onnx und .onnx.json", "Please choose both files: .onnx and .onnx.json"));
      return;
    }
    const name = model.name.replace(/\.onnx$/i, "").replace(/[^A-Za-z0-9_.-]/g, "_").replace(/\.\.+/g, ".").replace(/^[^A-Za-z0-9]+/, "").slice(0, 80);
    const up = voiceMenu.querySelector(".vm-upload");
    const note = up && up.querySelector(".mi-note");
    const show = (text) => { if (note) note.textContent = text; };
    if (up) up.disabled = true;
    try {
      const base = `/api/voices/upload/${encodeURIComponent(name)}`;
      show(L("Lade Konfiguration hoch …", "Uploading configuration …"));
      await putFile(`${base}/config`, config, () => {});
      await putFile(`${base}/model`, model, (p) => show(L(`Lade ${name} hoch … ${Math.floor(p * 100)} %`, `Uploading ${name} … ${Math.floor(p * 100)} %`)));
      toast(L(`✔ Stimme ${name} hinzugefügt`, `✔ Voice ${name} added`));
      openVoiceMenu();
    } catch (err) {
      toast(err.message);
      if (up) up.disabled = false;
      show(L("Hochladen fehlgeschlagen – nochmal versuchen?", "Upload failed – try again?"));
    }
  }

  voiceMenu.addEventListener("dragover", (e) => { e.preventDefault(); voiceMenu.classList.add("drop"); });
  voiceMenu.addEventListener("dragleave", () => voiceMenu.classList.remove("drop"));
  voiceMenu.addEventListener("drop", (e) => {
    e.preventDefault();
    voiceMenu.classList.remove("drop");
    uploadVoice([...e.dataTransfer.files]);
  });

  function closeVoiceMenu() { /* Stimmen stehen jetzt dauerhaft in den Einstellungen */ }
  $("pill-voice").onclick = () => openSheet("settings", "voice");

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
      sub: `${Math.round(pct)} %` + (c.trimmed ? L(" · gekürzt", " · trimmed") : c.summarized ? L(" · verdichtet", " · condensed") : ""),
      title: [
        L(`Prompt ca. ${c.used} von ${c.budget} Token Budget (Fenster ${c.window}, Rest bleibt für die Antwort)`,
          `Prompt approx. ${c.used} of ${c.budget} token budget (window ${c.window}, the rest is kept for the answer)`),
        `System ${p.system ?? "?"} · Tools ${p.tools ?? "?"} · ${L("Gedächtnis", "Memory")} ${p.memory ?? "?"} · `
          + `${L("Verlauf", "History")} ${p.history ?? "?"}`,
        c.real ? L("Laut Modell-Server: ", "According to the model server: ") + `${c.real} Token`
          + (c.cached ? L(`, davon ${c.cached} aus dem Cache (schneller)`, `, ${c.cached} of them from the cache (faster)`) : "") : "",
        c.summarized ? L("Älterer Verlauf ist in einer Zusammenfassung verdichtet – Details holt das Gedächtnis bei Bedarf zurück.",
                         "Older history is condensed into a summary – memory brings back details when needed.") : "",
        c.trimmed ? L("Ältere Teile/lange Tool-Ergebnisse wurden gekürzt, damit alles passt.",
                      "Older parts/long tool results were trimmed so everything fits.") : "",
      ].filter(Boolean).join("\n"),
    });
    $("tele-ctx").querySelector(".tele-num").textContent = kTok(c.used);
    $("tele-ctx").querySelector(".tele-unit").textContent = "/" + Math.round(c.budget / 1000) + "k";
    $("tele-ctx").classList.toggle("warn", c.trimmed || (pct >= 80 && pct < 95));
    orb.setContext(c.used / c.budget, !!c.summarized);
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
    closeVoiceMenu();
    let data;
    try { data = await getJSON("/api/models"); } catch { toast(L("Modelle nicht ladbar", "Could not load models")); return; }
    modelMenu.innerHTML = `<div class="mm-title">${L("MODELL WÄHLEN", "CHOOSE MODEL")}</div>`;
    for (const d of data.pulls || []) modelMenu.appendChild(pullRow(d));
    for (const p of data.profiles) {
      const b = document.createElement("button");
      b.className = "model-item" + (p.active ? " active" : "");
      b.disabled = !!data.switching;
      b.innerHTML = `<div class="mi-head"><span class="mi-name"></span><span class="mi-tag"></span></div><div class="mi-sub"></div>`;
      b.querySelector(".mi-name").textContent = p.label;
      b.querySelector(".mi-tag").textContent = p.active ? L("AKTIV", "ACTIVE")
        : p.managed ? L("STARTET SERVER", "STARTS SERVER") : p.backend.toUpperCase();
      b.querySelector(".mi-sub").textContent = `${p.backend} · ${p.model}` + (p.size_gb ? ` · ${p.size_gb} GB` : "");
      b.onclick = async () => {
        closeModelMenu();
        if (p.active) return;
        const r = await fetch(`/api/models/${encodeURIComponent(p.name)}/activate`, { method: "POST" });
        if (!r.ok && r.status !== 502) toast(L("Umschalten fehlgeschlagen", "Switching failed"));
      };
      if (!p.deletable) { modelMenu.appendChild(b); continue; }
      const row = document.createElement("div");
      row.className = "model-row";
      const del = document.createElement("button");
      del.className = "model-del";
      del.textContent = "🗑";
      del.disabled = p.active || !!data.switching;
      del.title = p.active ? L("Aktives Modell – erst ein anderes wählen", "Active model – choose another one first")
        : L("Modell löschen", "Delete model");
      del.onclick = (e) => { e.stopPropagation(); deleteModel(p); };
      row.append(b, del);
      modelMenu.appendChild(row);
    }
    if (data.active !== "demo") {
      const add = document.createElement("button");
      add.className = "model-item add";
      add.textContent = L("+ MODELL HINZUFÜGEN …", "+ ADD MODEL …");
      add.onclick = (e) => { e.stopPropagation(); openPresetMenu(); };
      modelMenu.appendChild(add);
    }
    modelMenu.classList.remove("hidden");
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
    for (const tag of data.pulling) modelMenu.appendChild(pullRow({ tag }));
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

  async function deleteModel(p) {
    const size = p.size_gb ? L(` Gibt ~${p.size_gb} GB frei.`, ` Frees ~${p.size_gb} GB.`) : "";
    if (!confirm(L(`${p.label} löschen? Die Modelldateien werden entfernt.`, `Delete ${p.label}? The model files are removed.`) + size)) return;
    try {
      await api("DELETE", `/api/models/${encodeURIComponent(p.name)}`);
      toast(L(`✔ ${p.label} gelöscht`, `✔ ${p.label} deleted`));
      openModelMenu();
    } catch { /* Meldung kommt von api() */ }
  }

  function pullPct(d) {
    return d.total ? Math.floor((100 * (d.completed || 0)) / d.total) : null;
  }

  // Laufender Download im Modell-Menü: Fortschritt live (model_pull-Events) und Abbrechen
  function pullRow(d) {
    const row = document.createElement("div");
    row.className = "model-pull";
    row.dataset.tag = d.tag;
    row.innerHTML = `<div class="mp-head"><span class="mp-text"></span>
      <button class="mp-cancel">✕ ${L("ABBRECHEN", "CANCEL")}</button></div><div class="mp-bar"><i></i></div>`;
    row.querySelector(".mp-cancel").onclick = async (e) => {
      e.stopPropagation();
      e.target.disabled = true;
      try { await api("DELETE", `/api/models/pull/${encodeURIComponent(d.tag)}`); } catch { /* Meldung kommt von api() */ }
    };
    updatePullRow(row, d);
    return row;
  }

  function updatePullRow(row, d) {
    const pct = pullPct(d);
    row.querySelector(".mp-text").textContent = `⬇ ${d.tag}` + (pct !== null ? ` · ${pct} %` : d.status ? ` · ${d.status}` : " …");
    row.querySelector(".mp-bar i").style.width = (pct || 0) + "%";
  }

  function modelPullEvent(ev) {
    const row = [...modelMenu.querySelectorAll(".model-pull")].find((r) => r.dataset.tag === ev.tag);
    if (row && (ev.done || ev.error || ev.cancelled)) row.remove();
    else if (row) updatePullRow(row, ev);
    if (ev.cancelled) { toast(L(`Download von ${ev.tag} abgebrochen`, `Download of ${ev.tag} cancelled`)); return; }
    if (ev.error) { toast(L(`✘ ${ev.tag}: `, `✘ ${ev.tag}: `) + ev.error); return; }
    if (ev.done) {
      toast(L(`✔ ${ev.tag} geladen – jetzt im Modell-Menü auswählbar.`, `✔ ${ev.tag} downloaded – now selectable in the model menu.`));
      addSystem(L(`Modell ${ev.tag} ist bereit (Menü LLM oben).`, `Model ${ev.tag} is ready (LLM menu at the top).`));
      return;
    }
    if (row) return;  // Menü offen: Fortschritt steht dort
    const pct = pullPct(ev) !== null ? ` ${pullPct(ev)} %` : "";
    toast(L(`Lade ${ev.tag}: `, `Downloading ${ev.tag}: `) + (ev.status || "") + pct
      + L(" · abbrechen im LLM-Menü", " · cancel in the LLM menu"));
  }

  function closeModelMenu() { /* Modelle stehen jetzt dauerhaft in den Einstellungen */ }
  $("pill-llm").onclick = () => openSheet("settings", "models");

  let toastTimer;
  function toast(text) {
    const el = $("toast");
    el.textContent = text;
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 5000);
  }

  // ---------------------------------------------------------------- REST
  // ---------------------------------------------------------------- Planer: Routinen
  const DAYS = L("Mo Di Mi Do Fr Sa So", "Mo Tu We Th Fr Sa Su").split(" ");
  const R = { items: [], edit: null, days: new Set() };

  function loadPlanner() {
    loadRoutines();
    loadReminders();
    if ($("brief-box").open) loadBriefing();
  }
  $("brief-box").addEventListener("toggle", () => { if ($("brief-box").open) loadBriefing(); });

  function renderDayChips() {
    const box = $("rt-days");
    box.innerHTML = "";
    DAYS.forEach((d, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "rt-day" + (R.days.has(i) ? " on" : "");
      b.textContent = d;
      b.onclick = () => { R.days.has(i) ? R.days.delete(i) : R.days.add(i); renderDayChips(); };
      box.appendChild(b);
    });
    const hint = document.createElement("span");
    hint.className = "rt-day-hint";
    hint.textContent = R.days.size ? "" : L("täglich", "daily");
    box.appendChild(hint);
  }

  function openRoutineForm(r) {
    R.edit = r ? r.id : null;
    R.days = new Set(r ? r.days : []);
    $("rt-name").value = r ? r.name : "";
    $("rt-task").value = r ? r.task : "";
    $("rt-time").value = r ? r.time : "08:00";
    $("rt-date").value = r ? r.date : "";
    $("rt-error").textContent = "";
    renderDayChips();
    $("rt-form").classList.remove("hidden");
    $("rt-new").classList.add("hidden");
    $("rt-task").focus();
  }

  function closeRoutineForm() {
    R.edit = null;
    $("rt-form").classList.add("hidden");
    $("rt-new").classList.remove("hidden");
  }

  async function loadRoutines() {
    try { R.items = await getJSON("/api/routines"); } catch { return; }
    const ul = $("rt-list");
    ul.innerHTML = "";
    if (!R.items.length) {
      ul.innerHTML = `<li class="empty">${L("Noch keine Routinen – z. B. „Werktags um 8 Linux-News suchen“.",
                                             "No routines yet – e.g. “Search Linux news on weekdays at 8”.")}</li>`;
      return;
    }
    for (const r of R.items) {
      const li = document.createElement("li");
      li.className = "rt-item" + (r.enabled ? "" : " off");
      const status = r.last_status || "none";
      li.innerHTML = `<input type="checkbox" ${r.enabled ? "checked" : ""} title="${L("aktiv / pausiert", "active / paused")}">
        <div class="rt-main"><div class="rt-name"><span class="rt-dot ${status}"></span><span class="n"></span></div>
          <div class="rt-meta"></div></div>
        <button class="ghost" data-a="run" title="${L("jetzt ausführen", "run now")}">▶</button>
        <button class="ghost" data-a="edit" title="${L("bearbeiten", "edit")}">✎</button>
        <button class="ghost" data-a="del" title="${L("löschen", "delete")}">✕</button>`;
      li.querySelector(".n").textContent = r.name;
      // „morgen 08:00“ → „morgen“, wenn die Uhrzeit ohnehin im Zeitplan steht
      const nextShort = r.next.endsWith(" " + r.time) ? r.next.slice(0, -r.time.length - 1) : r.next;
      const next = r.enabled && r.next !== "–" ? ` · ${nextShort}` : r.enabled ? "" : ` · ${L("pausiert", "paused")}`;
      li.querySelector(".rt-meta").textContent = r.schedule + next;
      li.querySelector(".rt-meta").title = r.enabled && r.next !== "–" ? `${L("nächste Ausführung", "next run")}: ${r.next}` : "";
      li.querySelector(".rt-main").title = r.task + (r.last_summary ? "\n\n" + L("Zuletzt: ", "Last: ") + r.last_summary : "");
      li.querySelector(".rt-main").onclick = () => {
        if (!r.chat_id) { toast(L("Noch kein Ergebnis – ▶ startet die Routine jetzt.", "No result yet – ▶ runs it now.")); return; }
        api("POST", `/api/chats/${r.chat_id}/activate`).catch(() => {});
      };
      li.querySelector("input").onchange = (e) => api("PUT", `/api/routines/${r.id}`, { enabled: e.target.checked })
        .then(loadRoutines).catch(() => toast(L("Speichern fehlgeschlagen", "Saving failed")));
      li.querySelectorAll("button").forEach((b) => b.onclick = async () => {
        if (b.dataset.a === "edit") return openRoutineForm(r);
        if (b.dataset.a === "del") {
          if (!confirm(L(`Routine „${r.name}“ löschen? Ihr Chat bleibt im Verlauf.`, `Delete routine “${r.name}”? Its chat stays in the history.`))) return;
          await api("DELETE", `/api/routines/${r.id}`).catch(() => {});
        } else {
          await api("POST", `/api/routines/${r.id}/run`).catch(() => {});
          toast(L(`Routine „${r.name}“ startet …`, `Starting routine “${r.name}” …`));
        }
        loadRoutines();
      });
      ul.appendChild(li);
    }
  }

  $("rt-new").onclick = () => openRoutineForm(null);
  $("rt-cancel").onclick = closeRoutineForm;
  $("rt-form").onsubmit = async (e) => {
    e.preventDefault();
    const body = { name: $("rt-name").value.trim(), task: $("rt-task").value.trim(), time: $("rt-time").value,
                   days: [...R.days].sort(), date: $("rt-date").value };
    if (!body.task) { $("rt-error").textContent = L("Bitte eine Aufgabe eintragen.", "Please enter a task."); return; }
    const r = await fetch(R.edit ? `/api/routines/${R.edit}` : "/api/routines", {
      method: R.edit ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) {
      $("rt-error").textContent = (await r.json().catch(() => ({}))).detail || L("Speichern fehlgeschlagen", "Saving failed");
      return;
    }
    closeRoutineForm();
    loadRoutines();
  };

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
    el.classList.remove("ok", "warn", "bad");
    el.classList.add(cls);
    el.querySelector("em").textContent = text;
  }

  async function loadStatus() {
    const asked = Date.now();
    try {
      const st = await getJSON("/api/status");
      S.status = st;
      if (st.name) document.querySelector(".brand-name").textContent = st.name;  // Persona (assistant_name)
      greet();
      const l = st.llm;
      const name = l.label && l.label !== l.model ? `${l.label} · ${l.model}` : l.model;
      // Nur eine Antwort, die nach dem letzten „Modell aktiv“ angefragt wurde, darf „lädt“ setzen – sonst bliebe
      // „Lade Modell“ nach dem Start stehen, bis man neu lädt
      if (l.switching && asked > S.modelDoneAt) { S.modelSwitching = S.modelSwitching || l.switching; refresh(); }
      else if (!l.switching && S.modelSwitching && asked > S.modelDoneAt) { S.modelSwitching = null; refresh(); }
      setPill("pill-llm", l.switching ? "warn" : !l.online ? "bad" : l.model_available ? "ok" : "warn",
        l.switching ? L("lädt …", "loading …") : !l.online ? `${l.label || l.model} offline` : l.model_available ? name : l.model + L(" fehlt", " missing"));
      const v = st.voice;
      const vCls = v.stt && v.tts ? "ok" : v.stt || v.tts ? "warn" : "bad";
      setPill("pill-voice", vCls, [v.stt ? "STT" : null, v.tts ? "TTS" : "TTS(Browser)", v.wake ? "WAKE" : null].filter(Boolean).join(" · "));
      setPill("pill-mem", "ok", `${st.memory.days} ${L("Tage", "days")} · ${st.memory.facts} ${L("Fakten", "facts")}`);
      const tg = st.telegram || {};
      setPill("pill-tg", tg.running ? "ok" : tg.error ? "bad" : "warn",
        tg.running ? (tg.bot || L("aktiv", "active")) : tg.error || (tg.configured === false ? L("nicht eingerichtet", "not set up") : L("startet …", "starting …")));
      if (tg.error && tg.error !== S.telegramError) toast("Telegram: " + tg.error);  // jedes Problem einmal melden
      S.telegramError = tg.error || "";
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
      li.querySelector(".m").textContent = `${chatWhen(c.updated)} · ${c.messages} ${L("Nachr.", "msgs")}` + (c.active ? L(" · aktiv", " · active") : "");
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
    $("chat-title").textContent = ev.title || "";
    loadHistory();
    loadChats();
  }

  async function loadHistory() {
    try {
      const h = await getJSON("/api/history");
      S.historyLoaded = true;
      setTimeout(renderBoot, 0);
      $("chat-title").textContent = h.chat && h.chat.title ? h.chat.title : "";
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

  // ---------------------------------------------------------------- Begrüßung (leerer Chat)
  function greet() {
    const h = new Date().getHours();
    const part = h < 5 ? L("Gute Nacht", "Good night") : h < 11 ? L("Guten Morgen", "Good morning")
      : h < 18 ? L("Guten Tag", "Good afternoon") : L("Guten Abend", "Good evening");
    const user = S.status && S.status.user ? ", " + S.status.user : "";
    $("greeting").textContent = `${part}${user} – ${L("wie kann ich helfen?", "how can I help?")}`;
  }
  setInterval(greet, 60000);
  greet();
  document.querySelectorAll(".suggestion").forEach((b) => {
    b.onclick = () => { $("input").value = b.dataset.text; $("form").requestSubmit(); };
  });

  // ---------------------------------------------------------------- Start: was lädt gerade?
  // Der Server meldet seine Schritte live (Ereignis „startup“); Verbindung und Chat-Verlauf prüft die Oberfläche selbst.
  const Boot = { server: null, entered: false };
  const BOOT_ICON = { ok: "✓", warn: "!", error: "✕", off: "–", running: "", pending: "" };
  function bootRows() {
    const rows = [{
      key: "conn", label: L("Verbindung", "Connection"), state: S.connected ? "ok" : "running",
      text: S.connected ? L("mit dem Orbwise-Server verbunden", "connected to the Orbwise server")
        : L("verbinde mit dem Orbwise-Server …", "connecting to the Orbwise server …"),
    }];
    if (Boot.server) rows.push(...Boot.server.steps);
    else rows.push({ key: "srv", label: L("Dienste", "Services"), state: "pending", text: L("warte auf den Server …", "waiting for the server …") });
    rows.push({
      key: "chat", label: "Chat", state: S.historyLoaded ? "ok" : S.connected ? "running" : "pending",
      text: S.historyLoaded ? ($("chat-title").textContent || L("neuer Chat", "new chat")) : L("lade den letzten Chat …", "loading the last chat …"),
    });
    return rows;
  }
  function renderBoot() {
    const model = Boot.server && Boot.server.steps.find((x) => x.key === "model");
    const loading = model && model.state === "running";
    if (S.startupModel && !loading) { S.modelSwitching = null; S.modelDoneAt = Date.now(); }
    S.startupModel = loading ? model.text : null;  // auch nach dem Start im Orb anzeigen
    Boot.startupLoad = !!loading;
    if (Boot.entered) { refresh(); return; }
    const rows = bootRows();
    const ul = $("boot-steps");
    ul.innerHTML = "";
    for (const r of rows) {
      const li = document.createElement("li");
      li.className = "boot-step " + r.state;
      li.innerHTML = `<span class="bs-icon">${BOOT_ICON[r.state] || ""}</span><span class="bs-label"></span><span class="bs-text"></span>`;
      li.querySelector(".bs-label").textContent = r.label;
      li.querySelector(".bs-text").textContent = r.text || "";
      li.title = r.text || "";
      ul.appendChild(li);
    }
    const done = rows.filter((r) => !["running", "pending"].includes(r.state)).length;
    $("boot-bar").style.width = Math.round((done / rows.length) * 100) + "%";
    const ready = done === rows.length;
    const btn = $("boot-btn");
    btn.disabled = !S.connected;
    btn.classList.toggle("primary", ready);
    btn.textContent = ready ? L("Orbwise starten", "Start Orbwise")
      : L("Schon starten – der Rest lädt im Hintergrund", "Start now – the rest keeps loading");
    const problems = rows.filter((r) => r.state === "error").length;
    $("boot-sub").textContent = !S.connected ? L("Warte auf den Orbwise-Server …", "Waiting for the Orbwise server …")
      : ready ? (problems ? L("Bereit – mit Hinweisen (siehe oben)", "Ready – with notes (see above)") : L("Alles bereit", "All set"))
        : L("Orbwise startet …", "Orbwise is starting …");
    $("boot").classList.toggle("ready", ready);
    // Alles geladen → ohne Klick weiter (Ton/Mikrofon gibt der Browser dann beim ersten Klick/Tastendruck frei)
    if (ready && !Boot.autoTimer) Boot.autoTimer = setTimeout(() => { if (!Boot.entered) enter(false); }, 700);
  }
  setInterval(() => { if (!Boot.entered) renderBoot(); }, 1000);

  async function enter(byClick) {
    if (Boot.entered) return;
    Boot.entered = true;
    await initAudio().catch(() => {});
    if (!byClick) {  // ohne Klick blockt der Browser Ton/Mikrofon bis zur ersten Interaktion – dann freigeben
      const unlock = () => { if (A.ctx && A.ctx.state === "suspended") A.ctx.resume().catch(() => {}); };
      document.addEventListener("pointerdown", unlock, { once: true });
      document.addEventListener("keydown", unlock, { once: true });
    }
    $("boot").classList.add("hidden");
    orb.boot();
    if (S.wake) {
      if (await initMic()) send({ type: "wake", enabled: true });
      else { S.wake = false; store.set("wake", false); }
      updateMicStreaming();
    }
    $("input").focus();
    refresh();
  }
  $("boot-btn").onclick = () => enter(true);

  renderBoot();
  connect();
  refresh();
})();
