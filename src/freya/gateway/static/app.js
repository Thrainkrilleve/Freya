"use strict";

// Model text is placed in text nodes. Rich display uses a restricted renderer;
// replies and tool results are never trusted HTML.

const $ = (id) => document.getElementById(id);

const messages = $("messages");
const gallery = $("gallery");
const banner = $("banner");
const imageWorking = $("image-working");
const consoleOutput = $("console-output");

const CONSOLE_LINE_LIMIT = 1000;
const CONSOLE_SHOWN_KEY = "freya.consoleShown";

let socket = null;
let retryDelay = 500;
let currentReply = null;
let status = {};
let consoleLines = [];
let replaying = false;

const WHO = {
  window: "You",
  typed: "You (console)",
  voice: "You (voice)",
};

// -- helpers ---------------------------------------------------------

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function nearBottom(box) {
  return box.scrollHeight - box.scrollTop - box.clientHeight < 80;
}

function addToChat(node) {
  const stick = nearBottom(messages);
  $("chat-empty").hidden = true;
  messages.appendChild(node);
  if (stick) messages.scrollTop = messages.scrollHeight;
}

function addMessage(kind, who, text) {
  const bubble = element("div", "message " + kind);
  bubble.appendChild(element("span", "who", who));
  const body = element("span", "body", text);
  bubble.appendChild(body);
  addToChat(bubble);
  return { bubble, body };
}

function send(message) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(message));
    return true;
  }
  return false;
}

function when(seconds) {
  return new Date(seconds * 1000).toLocaleString([], {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function remember(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch (error) {
    // Storage can be unavailable; the tab then just starts hidden.
  }
}

function recall(key) {
  try {
    return localStorage.getItem(key);
  } catch (error) {
    return null;
  }
}

// -- conversation ------------------------------------------------------

function finishReply() {
  if (currentReply) {
    currentReply.bubble.classList.remove("is-writing");
    if (!currentReply.body.textContent.trim()) currentReply.bubble.remove();
    else FreyaRichChat.render(currentReply.body, currentReply.body.textContent);
    currentReply = null;
  }
}

function showEvent(event) {
  switch (event.type) {
    case "user":
      finishReply();
      addMessage("from-user", WHO[event.source] || "You", event.text);
      break;

    case "assistant_start":
      finishReply();
      currentReply = addMessage("from-freya is-writing", "Freya", "");
      break;

    case "assistant_chunk": {
      if (!currentReply) {
        currentReply = addMessage("from-freya is-writing", "Freya", "");
      }
      const stick = nearBottom(messages);
      currentReply.body.textContent += event.text;
      if (stick) messages.scrollTop = messages.scrollHeight;
      break;
    }

    case "assistant_end":
      finishReply();
      break;

    case "source_response":
      finishReply();
      FreyaRichChat.sourceMessage(event, addToChat);
      break;

    case "notice":
      finishReply();
      addToChat(element("p", "notice", event.text));
      break;

    case "image_started":
      imageWorking.textContent = "Generating: " + event.prompt;
      imageWorking.hidden = false;
      break;

    case "image_ready":
      finishReply();
      addChatImage(event.image);
      // When an earlier conversation is being replayed, the gallery is
      // filled from its own list instead.
      if (!replaying) {
        imageWorking.hidden = true;
        addImage(event.image, true);
        if (!$("view-images").classList.contains("is-active")) {
          $("images-badge").hidden = false;
        }
      }
      break;

    case "image_failed":
      imageWorking.hidden = true;
      break;

    case "console":
      addConsoleLines([event.text]);
      break;

    case "voice":
      voiceResult(event);
      break;

    case "avatar":
      avatarResult(event);
      break;

    case "media":
      window.dispatchEvent(new CustomEvent("freya-media", {detail: event}));
      break;

    case "memory":
      memoryResult(event);
      break;

    case "cloud":
      window.dispatchEvent(new CustomEvent("freya-cloud", {detail: event}));
      break;

    // A question Freya puts to the owner, and its being closed. These
    // are handed straight to this window and are never replayed.
    case "cloud.prompt":
      window.dispatchEvent(new CustomEvent("freya-cloud-prompt", {detail: event}));
      break;

    case "cloud.prompt.closed":
      window.dispatchEvent(new CustomEvent("freya-cloud-prompt-closed", {detail: event}));
      break;

    case "status":
      showStatus(event.status);
      break;

    case "error":
      addToChat(element("p", "notice", event.text));
      break;
  }
}

// -- console ---------------------------------------------------------

function addConsoleLines(lines) {
  const stick = nearBottom(consoleOutput);
  consoleLines = consoleLines.concat(lines).slice(-CONSOLE_LINE_LIMIT);
  consoleOutput.textContent = consoleLines.join("\n");
  if (stick) consoleOutput.scrollTop = consoleOutput.scrollHeight;
}

function showConsoleTab(shown) {
  $("tab-console").hidden = !shown;
  remember(CONSOLE_SHOWN_KEY, shown ? "1" : null);
  if (shown) {
    showView("console");
    consoleOutput.scrollTop = consoleOutput.scrollHeight;
  } else if ($("view-console").classList.contains("is-active")) {
    showView("chat");
  }
}

function isCommand(text) {
  return text.startsWith("/") || text.startsWith("\\");
}

// "exit" or "quit" on its own, in any letter case, is the quit command,
// as it is in the terminal. A sentence that contains the word is not.
function isQuitWord(text) {
  const word = text.trim().toLowerCase();
  return word === "exit" || word === "quit";
}

// How much the next message matters for cloud work, when the owner
// set it to something other than the usual. It is sent just ahead of
// the message, on the same connection and naming the message it is
// for, and the controls go back to normal so that a choice is never
// carried over by accident.
let cloudValueRequests = 0;

function sendCloudValue(text) {
  const value = document.getElementById("chat-value");
  const urgent = document.getElementById("chat-urgent");

  if (!value || !urgent) return;
  if (value.value === "normal" && !urgent.checked) return;

  cloudValueRequests += 1;
  send({
    type: "cloud",
    action: "next",
    request: "v" + cloudValueRequests,
    value: value.value,
    urgent: urgent.checked,
    text,
  });
  value.value = "normal";
  urgent.checked = false;
}

// Typed text is a message for Freya unless it starts with a slash, in
// which case it is one of her commands. Opening the Console tab is the
// one command handled here in the window.
function submitText(text) {
  if (isQuitWord(text)) {
    return send({ type: "command", text: text.trim() });
  }

  if (!isCommand(text)) {
    sendCloudValue(text);
    return send({ type: "say", text });
  }

  const command = text.slice(1).trim().toLowerCase();

  if (command === "terminal" || command === "console") {
    showConsoleTab(true);
    return true;
  }

  return send({ type: "command", text });
}

// -- voice -----------------------------------------------------------
//
// The Voice tab is built from what each speech engine says it can do.
// A kind of voice work is only offered through engines that declare
// it, and a saved voice is only ever used with the engine that made it.

let voice = null;
let voiceRequests = 0;
let voicePending = null;

const KIND_NAMES = {
  preset: "Preset voice",
  blend: "Blend of supplied voices",
  clone: "Cloned from a recording",
  design: "Designed from a description",
};

const STATUS_NAMES = {
  ready: "Ready",
  not_installed: "Not installed",
  not_configured: "Not set up",
};

function engineById(id) {
  return ((voice && voice.engines) || []).find((engine) => engine.id === id);
}

function fillSelect(select, options, keep) {
  const previous = keep === undefined ? select.value : keep;
  select.replaceChildren();
  options.forEach((option) => {
    const item = element("option", "", option.label);
    item.value = option.value;
    item.disabled = Boolean(option.disabled);
    select.appendChild(item);
  });
  if (options.some((o) => o.value === previous && !o.disabled)) {
    select.value = previous;
  } else {
    const first = options.find((o) => !o.disabled);
    if (first) select.value = first.value;
  }
}

function voiceMessage(text, isError) {
  const line = $("voice-message");
  line.textContent = text;
  line.classList.toggle("is-error", Boolean(isError));
}

// `then` runs with the answer when the request succeeds.
function voiceSend(action, fields, working, then) {
  voiceRequests += 1;
  const request = "v" + voiceRequests;
  if (send({ type: "voice", action, request, ...fields })) {
    voicePending = { request, then };
    voiceMessage(working || "Working…");
  }
}

function voiceResult(event) {
  showVoice(event.state);

  // Another window's request only refreshes what is shown here.
  if (!voicePending || event.request !== voicePending.request) return;
  const then = voicePending.then;
  voicePending = null;

  if (event.error) {
    voiceMessage(event.error, true);
    return;
  }

  if (event.clip) {
    const player = $("voice-player");
    const source = "/audio/" + encodeURIComponent(event.clip.id);
    player.src = source;
    player.hidden = false;
    player.play().catch(() => {});
    const link = $("voice-download");
    link.href = source;
    link.hidden = false;
    voiceMessage(event.clip.seconds + " seconds of speech.");
  } else {
    voiceMessage("Done.");
  }

  if (then) then(event);
}

function languageOptions(engine) {
  return engine.controls.languages.map((language) => ({
    value: language.code,
    label: language.available
      ? language.name
      : language.name + " (needs extra language packages)",
    disabled: !language.available,
  }));
}

function voiceOptions(engine, code) {
  const language = engine.controls.languages.find((l) => l.code === code);
  return ((language && language.voices) || []).map((v) => ({
    value: v.id,
    label: v.label,
  }));
}

function engineRow(engine) {
  const row = element("li");
  const about = element("div", "about");
  about.appendChild(element("span", "name", engine.name));
  about.appendChild(element("span", "detail", engine.summary));
  if (engine.detail) about.appendChild(element("span", "detail", engine.detail));
  row.appendChild(about);
  if (!engine.local) row.appendChild(element("span", "state is-cloud", "Cloud service"));
  row.appendChild(
    element(
      "span",
      "state" + (engine.status === "ready" ? " is-ready" : ""),
      STATUS_NAMES[engine.status] || engine.status,
    ),
  );
  return row;
}

function fillEngineList(listId, ids) {
  const list = $(listId);
  list.replaceChildren();
  ids.map(engineById).filter(Boolean).forEach((engine) => {
    list.appendChild(engineRow(engine));
  });
  if (!list.children.length) {
    list.appendChild(element("li", "", "No engine offers this yet."));
  }
}

function presetEngine() {
  return ((voice && voice.sections.preset) || [])
    .map(engineById)
    .find((engine) => engine && engine.status === "ready");
}

function blendEngine() {
  return ((voice && voice.sections.blend) || [])
    .map(engineById)
    .find((engine) => engine && engine.status === "ready");
}

function refreshPresetVoices() {
  const engine = presetEngine();
  if (engine) {
    fillSelect($("preset-voice"), voiceOptions(engine, $("preset-language").value));
  }
}

function refreshBlendVoices() {
  const engine = blendEngine();
  if (!engine) return;
  const options = voiceOptions(engine, $("blend-language").value);
  fillSelect($("blend-a"), options);
  fillSelect($("blend-b"), options);
  if ($("blend-a").value === $("blend-b").value && options.length > 1) {
    $("blend-b").value = options[1].value;
  }
}

function refreshPlaygroundProfiles() {
  const profiles = ((voice && voice.profiles) || []).filter(
    (profile) => profile.engine === $("play-engine").value,
  );
  fillSelect(
    $("play-profile"),
    profiles.length
      ? profiles.map((p) => ({ value: p.id, label: p.name }))
      : [{ value: "", label: "No saved voices for this engine yet", disabled: true }],
  );
  $("play-speak").disabled = !profiles.length;
}

function profileRow(profile) {
  const engine = engineById(profile.engine);
  const row = element("li");
  const about = element("div", "about");
  about.appendChild(element("span", "name", profile.name));
  about.appendChild(
    element(
      "span",
      "detail",
      (KIND_NAMES[profile.kind] || profile.kind) +
        " · " +
        (engine ? engine.name : profile.engine) +
        " only",
    ),
  );
  row.appendChild(about);

  const isActive = voice.active_profile === profile.id;
  const use = element(
    "button",
    isActive ? "primary" : "ghost",
    isActive ? "Freya's voice" : "Use for Freya",
  );
  use.type = "button";
  use.disabled = !profile.can_be_freyas_voice;
  use.title = profile.can_be_freyas_voice
    ? isActive
      ? "Click to go back to her default voice."
      : "Make this the voice Freya speaks with."
    : "Freya speaks English with Kokoro, so only an English Kokoro voice can be hers.";
  use.addEventListener("click", () => {
    voiceSend("use", { profile: isActive ? null : profile.id }, "Changing Freya's voice…");
  });
  row.appendChild(use);

  const remove = element("button", "ghost", "Delete");
  remove.type = "button";
  remove.addEventListener("click", () => {
    voiceSend("delete", { profile: profile.id }, "Deleting…");
  });
  row.appendChild(remove);

  return row;
}

// -- cloning and designing ---------------------------------------------

// A recording is decoded here, in the page, and sent to Freya as plain
// mono samples, so she never has to parse an audio file she was given.
const RECORDING_RATE = 24000;
const RECORDING_CHUNK = 48 * 1024;
const CLONE_SLIDERS = ["temperature", "exaggeration", "cfg_weight"];
const SLOW_NOTE = " The first time takes longer while the engine loads.";

// The recording Freya already holds for the chosen file, and the voice
// that was last designed.
let cloneReference = null;
let designReference = null;

function readyEngines(section) {
  return ((voice && voice.sections[section]) || [])
    .map(engineById)
    .filter((engine) => engine && engine.status === "ready");
}

function engineOptions(engines) {
  return engines.map((engine) => ({ value: engine.id, label: engine.name }));
}

function cloneModel() {
  const engine = engineById($("clone-engine").value);
  const models = (engine && engine.controls.models) || [];
  return models.find((model) => model.id === $("clone-model").value) || models[0];
}

function refreshCloneControls() {
  const engine = engineById($("clone-engine").value);
  const model = cloneModel();
  if (!engine || !model) return;

  fillSelect(
    $("clone-language"),
    model.languages.map((l) => ({ value: l.code, label: l.name })),
  );

  CLONE_SLIDERS.forEach((name) => {
    const range = engine.controls[name];
    const shown = Boolean(range) && model.controls.includes(name);
    $("clone-" + name + "-field").hidden = !shown;
    if (!shown) return;

    const input = $("clone-" + name);
    input.min = range.min;
    input.max = range.max;
    input.step = range.step;
    if (!input.dataset.touched) input.value = range.default;
    $("clone-" + name + "-value").textContent = Number(input.value).toFixed(2);
  });

  const reference = engine.controls.reference || {};
  $("clone-transcript-field").hidden = !reference.transcript;
}

function refreshCloneModels() {
  const engine = engineById($("clone-engine").value);
  if (!engine) return;
  fillSelect(
    $("clone-model"),
    (engine.controls.models || []).map((m) => ({ value: m.id, label: m.name })),
  );
  refreshCloneControls();
}

function refreshDesignControls() {
  const engine = engineById($("design-engine").value);
  if (!engine) return;
  fillSelect(
    $("design-language"),
    (engine.controls.languages || []).map((l) => ({ value: l.code, label: l.name })),
  );
  fillSelect(
    $("design-model"),
    (engine.controls.models || []).map((m) => ({ value: m.id, label: m.name })),
  );
}

// The imported recording, decoded: { name, stamp, audio }.
let cloneClip = null;
let cloneListening = null;

function recordingLimits() {
  return (voice && voice.reference_seconds) || { min: 6, max: 30 };
}

function cloneStart() {
  return Number($("clone-start").value);
}

function describeCloneClip() {
  const limits = recordingLimits();
  const note = $("clone-file-note");

  if (!cloneClip) {
    note.textContent =
      "Choose an audio file or drop one here: " +
      limits.min + " to " + limits.max + " seconds of one person speaking.";
    return;
  }

  const length = cloneClip.audio.duration;
  const from = cloneStart();
  const to = Math.min(length, from + limits.max);
  $("clone-start-value").textContent = from.toFixed(1) + " s";
  note.textContent =
    cloneClip.name + " · " + length.toFixed(1) + " seconds long · using " +
    from.toFixed(1) + " to " + to.toFixed(1) + " s" +
    (length < limits.min ? " · too short to clone from" : "");
}

function stopListening() {
  if (cloneListening) {
    cloneListening.close();
    cloneListening = null;
  }
  $("clone-listen").textContent = "Play the part that will be used";
}

// Take in an audio file the owner chose or dropped. It is decoded
// here in the page; nothing is sent to Freya until Preview or Save.
async function importRecording(file) {
  if (!file) return;
  stopListening();
  $("clone-file-note").textContent = "Reading " + file.name + "…";

  const context = new AudioContext();
  try {
    const audio = await context.decodeAudioData(await file.arrayBuffer());
    cloneClip = {
      name: file.name,
      stamp: [file.name, file.size, file.lastModified].join("|"),
      audio,
    };
  } catch (error) {
    cloneClip = null;
    $("clone-listen").hidden = true;
    $("clone-start").disabled = true;
    $("clone-file-note").textContent =
      file.name + " could not be read as audio. Try a WAV, MP3, M4A, FLAC or OGG file.";
    return;
  } finally {
    context.close();
  }

  const slider = $("clone-start");
  const latest = Math.max(0, cloneClip.audio.duration - recordingLimits().min);
  slider.max = Math.floor(latest * 2) / 2;
  slider.value = 0;
  slider.disabled = Number(slider.max) === 0;
  $("clone-listen").hidden = false;
  describeCloneClip();
}

function listenToRecording() {
  if (cloneListening) {
    stopListening();
    return;
  }
  if (!cloneClip) return;

  const context = new AudioContext();
  const source = context.createBufferSource();
  source.buffer = cloneClip.audio;
  source.connect(context.destination);
  source.onended = stopListening;
  source.start(0, cloneStart(), recordingLimits().max);
  cloneListening = context;
  $("clone-listen").textContent = "Stop";
}

// The chosen part of the recording as mono 16-bit samples.
async function recordingSamples() {
  const from = cloneStart();
  const seconds = Math.min(cloneClip.audio.duration - from, recordingLimits().max);
  const frames = Math.max(Math.floor(seconds * RECORDING_RATE), 1);
  const offline = new OfflineAudioContext(1, frames, RECORDING_RATE);
  const source = offline.createBufferSource();
  source.buffer = cloneClip.audio;
  source.connect(offline.destination);
  source.start(0, from);
  const samples = (await offline.startRendering()).getChannelData(0);
  const pcm = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i += 1) {
    pcm[i] = Math.max(-1, Math.min(1, samples[i])) * 32767;
  }
  return pcm;
}

function wireImport() {
  const drop = $("clone-drop");
  const picker = $("clone-file");

  $("clone-import").addEventListener("click", () => picker.click());
  picker.addEventListener("change", () => {
    importRecording(picker.files[0]);
    // So that choosing the same file again is noticed.
    picker.value = "";
  });
  $("clone-listen").addEventListener("click", listenToRecording);
  $("clone-start").addEventListener("input", () => {
    stopListening();
    describeCloneClip();
  });

  ["dragenter", "dragover"].forEach((name) => {
    drop.addEventListener(name, (event) => {
      event.preventDefault();
      drop.classList.add("is-over");
    });
  });
  ["dragleave", "drop"].forEach((name) => {
    drop.addEventListener(name, (event) => {
      event.preventDefault();
      drop.classList.remove("is-over");
    });
  });
  drop.addEventListener("drop", (event) => {
    importRecording(event.dataTransfer.files[0]);
  });

  // A file dropped anywhere else must not replace Freya's window.
  ["dragover", "drop"].forEach((name) => {
    window.addEventListener(name, (event) => event.preventDefault());
  });
}

// Make sure Freya holds the chosen recording, then carry on with its id.
function withCloneRecording(next) {
  if (!cloneClip) {
    voiceMessage("Import a recording first.", true);
    return;
  }
  if (!$("clone-consent").checked) {
    voiceMessage("Confirm that you have the right to use this voice first.", true);
    return;
  }

  const transcript = $("clone-transcript").value;
  const key = [cloneClip.stamp, cloneStart(), transcript].join("|");
  if (cloneReference && cloneReference.key === key) {
    next(cloneReference.id);
    return;
  }

  stopListening();
  voiceMessage("Preparing the recording…");
  recordingSamples()
    .then((pcm) => {
      const bytes = pcm.buffer;
      for (let at = 0; at < bytes.byteLength; at += RECORDING_CHUNK) {
        socket.send(bytes.slice(at, at + RECORDING_CHUNK));
      }
      voiceSend(
        "reference",
        { sample_rate: RECORDING_RATE, consent: true, transcript },
        "Sending the recording…",
        (event) => {
          cloneReference = { key, id: event.reference.id };
          next(event.reference.id);
        },
      );
    })
    .catch(() => {
      voiceMessage("That recording could not be prepared.", true);
    });
}

function cloneSettings(reference) {
  const model = cloneModel();
  const settings = {
    model: model.id,
    language: $("clone-language").value,
    reference,
  };
  CLONE_SLIDERS.forEach((name) => {
    if (!$("clone-" + name + "-field").hidden) {
      settings[name] = Number($("clone-" + name).value);
    }
  });
  return settings;
}

function wireCloneAndDesign() {
  $("clone-engine").addEventListener("change", refreshCloneModels);
  $("clone-model").addEventListener("change", refreshCloneControls);
  $("design-engine").addEventListener("change", refreshDesignControls);

  CLONE_SLIDERS.forEach((name) => {
    const input = $("clone-" + name);
    input.addEventListener("input", () => {
      input.dataset.touched = "1";
      $("clone-" + name + "-value").textContent = Number(input.value).toFixed(2);
    });
  });

  $("clone-preview").addEventListener("click", () => {
    withCloneRecording((reference) => {
      voiceSend(
        "preview",
        {
          engine: $("clone-engine").value,
          kind: "clone",
          settings: cloneSettings(reference),
          text: $("clone-text").value,
        },
        "Generating speech in the cloned voice…" + SLOW_NOTE,
      );
    });
  });

  $("clone-save").addEventListener("click", () => {
    withCloneRecording((reference) => {
      voiceSend(
        "save",
        {
          engine: $("clone-engine").value,
          kind: "clone",
          settings: cloneSettings(reference),
          name: $("clone-name").value,
        },
        "Saving…",
      );
    });
  });

  $("design-generate").addEventListener("click", () => {
    const description = $("design-description").value;
    const language = $("design-language").value;
    const engine = $("design-engine").value;
    voiceSend(
      "design",
      {
        engine,
        settings: { description, language },
        text: $("design-text").value,
      },
      "Designing the voice…" + SLOW_NOTE,
      (event) => {
        designReference = { id: event.reference.id, engine, description, language };
        $("design-save").disabled = false;
        $("design-save").title = "";
      },
    );
  });

  $("design-save").addEventListener("click", () => {
    if (!designReference) return;
    voiceSend(
      "save",
      {
        engine: designReference.engine,
        kind: "design",
        name: $("design-name").value,
        settings: {
          description: designReference.description,
          language: designReference.language,
          reference: designReference.id,
          model: $("design-model").value,
        },
      },
      "Saving…",
      () => {
        designReference = null;
        $("design-save").disabled = true;
      },
    );
  });

  $("voice-free").addEventListener("click", () => {
    voiceSend("free", {}, "Unloading…");
  });
}

function showCloneAndDesign() {
  const cloners = readyEngines("clone");
  $("clone-form").hidden = !cloners.length;
  if (cloners.length) {
    fillSelect($("clone-engine"), engineOptions(cloners));
    refreshCloneModels();
    describeCloneClip();
    if (!$("clone-text").value) $("clone-text").value = voice.sample_text;
  }

  const designers = readyEngines("design");
  $("design-form").hidden = !designers.length;
  if (designers.length) {
    fillSelect($("design-engine"), engineOptions(designers));
    refreshDesignControls();
    if (!$("design-text").value) $("design-text").value = voice.sample_text;
  }

  $("voice-free").hidden = !voice.engines.some((engine) => engine.loaded);
}

function showVoice(next) {
  if (!next || !next.engines) return;
  voice = next;
  showCloneAndDesign();

  const preset = presetEngine();
  $("panel-preset").hidden = !preset;
  if (preset) {
    $("preset-engines").textContent = preset.name + ": " + preset.summary;
    fillSelect($("preset-language"), languageOptions(preset));
    refreshPresetVoices();
  }

  const blend = blendEngine();
  $("panel-blend").hidden = !blend;
  if (blend) {
    fillSelect($("blend-language"), languageOptions(blend));
    refreshBlendVoices();
  }

  fillEngineList("clone-engines", voice.sections.clone);
  fillEngineList("design-engines", voice.sections.design);

  fillSelect(
    $("play-engine"),
    voice.sections.playground
      .map(engineById)
      .filter(Boolean)
      .map((engine) => ({ value: engine.id, label: engine.name })),
  );
  refreshPlaygroundProfiles();
  if (!$("play-text").value) $("play-text").value = voice.sample_text;

  const list = $("profile-list");
  list.replaceChildren();
  voice.profiles.forEach((profile) => list.appendChild(profileRow(profile)));
  if (!voice.profiles.length) {
    list.appendChild(element("li", "", "No saved voices yet."));
  }
}

function presetSettings() {
  return {
    language: $("preset-language").value,
    voice: $("preset-voice").value,
    speed: Number($("preset-speed").value),
  };
}

function blendSettings() {
  const share = Number($("blend-mix").value);
  return {
    language: $("blend-language").value,
    speed: Number($("blend-speed").value),
    voices: [
      { voice: $("blend-a").value, weight: share },
      { voice: $("blend-b").value, weight: 100 - share },
    ],
  };
}

function wireVoice() {
  const speedLabel = (inputId, labelId) => {
    const update = () => {
      $(labelId).textContent = Number($(inputId).value).toFixed(2) + "x";
    };
    $(inputId).addEventListener("input", update);
    update();
  };
  speedLabel("preset-speed", "preset-speed-value");
  speedLabel("blend-speed", "blend-speed-value");

  $("blend-mix").addEventListener("input", () => {
    const share = Number($("blend-mix").value);
    $("blend-mix-value").textContent = share + "% / " + (100 - share) + "%";
  });

  $("preset-language").addEventListener("change", refreshPresetVoices);
  $("blend-language").addEventListener("change", refreshBlendVoices);
  $("play-engine").addEventListener("change", refreshPlaygroundProfiles);

  $("preset-preview").addEventListener("click", () => {
    const engine = presetEngine();
    if (engine) {
      voiceSend(
        "preview",
        { engine: engine.id, kind: "preset", settings: presetSettings() },
        "Generating a preview…",
      );
    }
  });

  $("preset-save").addEventListener("click", () => {
    const engine = presetEngine();
    if (engine) {
      voiceSend(
        "save",
        {
          engine: engine.id,
          kind: "preset",
          settings: presetSettings(),
          name: $("preset-name").value,
        },
        "Saving…",
      );
    }
  });

  $("blend-preview").addEventListener("click", () => {
    const engine = blendEngine();
    if (engine) {
      voiceSend(
        "preview",
        { engine: engine.id, kind: "blend", settings: blendSettings() },
        "Generating a preview…",
      );
    }
  });

  $("blend-save").addEventListener("click", () => {
    const engine = blendEngine();
    if (engine) {
      voiceSend(
        "save",
        {
          engine: engine.id,
          kind: "blend",
          settings: blendSettings(),
          name: $("blend-name").value,
        },
        "Saving…",
      );
    }
  });

  $("play-speak").addEventListener("click", () => {
    voiceSend(
      "speak",
      { profile: $("play-profile").value, text: $("play-text").value },
      "Generating speech…",
    );
  });
}

// -- avatar ----------------------------------------------------------
//
// The Avatar tab shows and changes the on-screen avatar's settings. It
// only ever asks: every request is checked by Freya, and her answer is
// what is shown.

let avatar = null;
let avatarRequests = 0;
let avatarPending = null;
let avatarMapStart = null;

const AVATAR_CHECKS = {
  "avatar-ontop": "always_on_top",
  "avatar-clickthrough": "click_through",
  "avatar-smooth": "smooth",
  "avatar-autoscale": "auto_scale",
};

const AVATAR_BOUNDS = ["left", "right", "top", "bottom"];

function avatarMessage(text, isError) {
  const line = $("avatar-message");
  line.textContent = text;
  line.classList.toggle("is-error", Boolean(isError));
}

function avatarSend(action, fields, working) {
  avatarRequests += 1;
  const request = "a" + avatarRequests;
  if (send({ type: "avatar", action, request, ...fields })) {
    avatarPending = request;
    avatarMessage(working || "Working…");
  }
}

function avatarResult(event) {
  if (event.state) showAvatar(event.state);

  if (!event.request || event.request !== avatarPending) return;
  avatarPending = null;
  avatarMessage(event.error || avatarSummary(), Boolean(event.error));
}

function avatarSummary() {
  if (!avatar) return "";
  if (avatar.config && avatar.config.enabled === false) {
    return "The avatar is turned off. Nothing will show or start it until you turn it on.";
  }
  if (avatar.error) return avatar.error;
  if (!avatar.running || !avatar.state || !avatar.state.visible) {
    return "The avatar is not showing.";
  }
  const state = avatar.state;
  let text =
    "Showing at " + Math.round(state.size_percent) +
    "% of the display's usable height.";
  if (state.pointing > 0.5) {
    text += state.covers_target
      ? " Pointing, but there was nowhere to stand that left the target uncovered."
      : " Pointing.";
  }
  return text;
}

function setRange(id, value, low, high) {
  const input = $(id);
  if (low !== undefined) input.min = low;
  if (high !== undefined) input.max = high;
  // Leave a slider alone while it is being dragged.
  if (document.activeElement !== input) input.value = value;
  $(id + "-value").textContent = Math.round(Number(input.value)) + "%";
}

function showAvatar(next) {
  if (!next || !next.config) return;
  avatar = next;
  const config = avatar.config;
  const range = avatar.size_range;

  fillSelect(
    $("avatar-appearance"),
    avatar.appearances.map((a) => ({ value: a.id, label: a.name })),
    config.appearance,
  );
  fillSelect(
    $("avatar-expression"),
    avatar.expressions.map((e) => ({ value: e, label: e })),
    (avatar.state && avatar.state.expression) || undefined,
  );
  fillSelect(
    $("avatar-display"),
    avatar.displays.map((d, index) => ({
      value: d.id,
      label:
        "Display " + (index + 1) + (d.primary ? " (main)" : "") + ", " +
        Math.round(d.bounds.right - d.bounds.left) + " × " +
        Math.round(d.bounds.bottom - d.bounds.top),
    })),
  );
  fillSelect(
    $("avatar-target"),
    avatar.targets.map((target) => ({ value: target.id, label: target.label })),
  );

  const size = (avatar.state && avatar.state.size_percent) ||
    config.current_size || config.size.preferred;
  setRange("avatar-size", size, config.size.minimum, config.size.maximum);
  setRange("avatar-min", config.size.minimum, range.smallest, range.largest);
  setRange("avatar-max", config.size.maximum, range.smallest, range.largest);
  setRange("avatar-preferred", config.size.preferred, config.size.minimum, config.size.maximum);

  Object.entries(AVATAR_CHECKS).forEach(([id, key]) => {
    $(id).checked = Boolean(config[key]);
  });

  AVATAR_BOUNDS.forEach((edge) => {
    setRange("avatar-bound-" + edge, config.boundary[edge] * 100);
  });

  // While she is turned off, nothing that would show her can be asked for.
  const off = config.enabled === false;
  $("avatar-power").textContent = off ? "Turn on" : "Turn off";
  ["avatar-show", "avatar-hide", "avatar-stop", "avatar-point", "avatar-wave",
    "avatar-idle", "avatar-rest"].forEach((id) => {
    $(id).disabled = off;
  });

  const chosen = avatar.appearances.find((a) => a.id === config.appearance);
  $("avatar-summary").textContent =
    (chosen ? chosen.summary + " " : "") + (avatar.notes || []).join(" ");

  const list = $("avatar-permissions");
  list.replaceChildren();
  avatar.permissions.forEach((permission) => {
    const row = element("li");
    const about = element("div", "about");
    about.appendChild(element("span", "name", permission.name));
    about.appendChild(element("span", "detail", permission.id));
    row.appendChild(about);
    row.appendChild(
      element(
        "span",
        "state" + (permission.built ? " is-ready" : ""),
        permission.built ? "Exists" : "Not built",
      ),
    );
    list.appendChild(row);
  });

  showAvatarMap();
  showLibrary();
  if (!avatarPending) avatarMessage(avatarSummary(), Boolean(avatar.error));
}

// -- memory ----------------------------------------------------------
//
// Searching, correcting, and deleting what Freya has saved. Everything
// shown here is text someone typed or said, so it is only ever placed
// on the page as text.

let memoryRequests = 0;
let memoryPending = null;
let memoryQuery = "";
let memoryLoaded = false;

function memoryMessage(text, isError) {
  const line = $("memory-message");
  line.textContent = text;
  line.classList.toggle("is-error", Boolean(isError));
}

function memorySend(action, fields, working) {
  memoryRequests += 1;
  const request = "m" + memoryRequests;
  if (send({ type: "memory", action, request, query: memoryQuery, ...fields })) {
    memoryPending = request;
    memoryMessage(working || "Looking…");
  }
}

function memoryButton(label, title, onClick) {
  const button = element("button", "ghost", label);
  button.type = "button";
  if (title) button.title = title;
  button.addEventListener("click", onClick);
  return button;
}

function shortDate(text) {
  return String(text || "").slice(0, 16).replace("T", " ");
}

function memoryRow(memory) {
  const row = element("li");
  const about = element("div", "about");
  about.appendChild(element("span", "said", memory.content));
  about.appendChild(
    element(
      "span",
      "detail",
      memory.type + (memory.key ? " · " + memory.key : "") + " · memory " + memory.id,
    ),
  );
  row.appendChild(about);

  row.appendChild(
    memoryButton("Edit", "Change what this memory says.", () => {
      const editor = element("textarea");
      editor.value = memory.content;
      editor.maxLength = 2000;
      editor.rows = 3;
      editor.setAttribute("aria-label", "The memory's new text");
      const save = memoryButton("Save", "", () => {
        memorySend("memory-edit", { id: memory.id, content: editor.value }, "Saving…");
      });
      save.className = "primary";
      const cancel = memoryButton("Cancel", "", () => row.replaceWith(memoryRow(memory)));
      row.replaceChildren(editor, save, cancel);
      editor.focus();
    }),
  );
  row.appendChild(
    memoryButton("Delete", "Delete this memory.", () => {
      memorySend("memory-delete", { id: memory.id }, "Deleting…");
    }),
  );
  return row;
}

function messageRow(turn, isMatch) {
  const row = element("li", isMatch ? "is-match" : "");
  const about = element("div", "about");
  about.appendChild(element("span", "who", turn.role === "user" ? "You" : "Freya"));
  about.appendChild(element("span", "said", turn.content));
  row.appendChild(about);
  row.appendChild(
    memoryButton("Delete", "Delete this message and the other half of its exchange, for good.", () => {
      memorySend("turn-delete", { id: turn.id }, "Deleting…");
    }),
  );
  return row;
}

function passageRow(passage) {
  const row = element("li", "passage");
  const head = element("div", "passage-head");
  head.appendChild(
    element(
      "span",
      "detail",
      "Conversation " + passage.conversation + " · " + shortDate(passage.started_at),
    ),
  );
  head.appendChild(
    memoryButton("Open", "Read the whole conversation.", () => {
      memorySend("conversation-read", { id: passage.conversation }, "Opening…");
    }),
  );
  row.appendChild(head);
  const list = element("ul", "memory-list");
  passage.turns.forEach((turn) => list.appendChild(messageRow(turn, turn.id === passage.match)));
  row.appendChild(list);
  return row;
}

function conversationRow(conversation) {
  const row = element("li");
  const about = element("div", "about");
  about.appendChild(
    element(
      "span",
      "said",
      conversation.summary || "Not summarised yet.",
    ),
  );
  about.appendChild(
    element(
      "span",
      "detail",
      "Conversation " + conversation.id + " · " + shortDate(conversation.started_at) +
        " · " + conversation.turns + " messages" + (conversation.current ? " · this one" : ""),
    ),
  );
  row.appendChild(about);
  row.appendChild(
    memoryButton("Open", "Read the whole conversation.", () => {
      memorySend("conversation-read", { id: conversation.id }, "Opening…");
    }),
  );
  row.appendChild(
    memoryButton("Delete", "Delete this whole conversation for good.", () => {
      memorySend("conversation-delete", { id: conversation.id }, "Deleting…");
    }),
  );
  return row;
}

function fillRows(id, items, make, empty) {
  const list = $(id);
  list.replaceChildren();
  items.forEach((item) => list.appendChild(make(item)));
  if (!items.length && empty) list.appendChild(element("li", "", empty));
}

function memoryResult(event) {
  if (event.request !== memoryPending) return;
  memoryPending = null;
  memoryLoaded = true;

  const memories = event.memories || [];
  const passages = event.passages || [];
  const searching = Boolean(event.query);

  $("memory-count").textContent = memories.length ? "(" + memories.length + ")" : "";
  fillRows(
    "memory-memories",
    memories,
    memoryRow,
    searching ? "No saved memory matches." : "No memories saved yet.",
  );

  $("memory-passages-panel").hidden = !searching;
  fillRows("memory-passages", passages, passageRow, "Nothing in past conversations matches.");
  fillRows(
    "memory-conversations",
    event.conversations || [],
    conversationRow,
    "No conversations saved yet.",
  );

  const opened = event.opened;
  $("memory-opened").hidden = !opened;
  if (opened) {
    $("memory-opened-title").textContent =
      "Conversation " + opened.id + " · " + shortDate(opened.started_at);
    $("memory-opened-summary").textContent = opened.summary
      ? "Summary: " + opened.summary
      : "";
    fillRows("memory-opened-messages", opened.messages || [], (turn) => messageRow(turn, false));
    $("memory-opened").scrollIntoView({ block: "nearest" });
  }

  memoryMessage(
    event.error ||
      (searching
        ? memories.length + " memories and " + passages.length +
          " conversation passages match."
        : ""),
    Boolean(event.error),
  );
}

function wireMemory() {
  $("memory-form").addEventListener("submit", (submitted) => {
    submitted.preventDefault();
    memoryQuery = $("memory-query").value.trim();
    memorySend("search", {});
  });
  $("memory-all").addEventListener("click", () => {
    memoryQuery = "";
    $("memory-query").value = "";
    memorySend("search", {});
  });
  // Look once, the first time the tab is opened.
  $("tab-memory").addEventListener("click", () => {
    if (!memoryLoaded) memorySend("search", {});
  });
}

// -- avatar models ---------------------------------------------------
//
// Everything shown about a model came out of a file someone else made,
// so it is only ever placed on the page as text.

let previewed = null;
let libraryScanned = false;

function megabytes(bytes) {
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function modelFacts(model) {
  const stats = model.stats || {};
  return [
    "VRM " + model.spec,
    megabytes(model.size),
    (stats.triangles || 0).toLocaleString() + " triangles",
    (model.expressions || []).length + " expressions",
  ].join(" · ");
}

function modelRow(model) {
  const row = element("li");

  if (model.has_thumbnail) {
    const picture = element("img");
    picture.src = "/avatar-thumbnails/" + encodeURIComponent(model.sha256);
    picture.alt = "";
    picture.loading = "lazy";
    row.appendChild(picture);
  } else {
    row.appendChild(element("div", "no-thumbnail"));
  }

  const about = element("div", "about");
  about.appendChild(element("span", "name", model.name));
  about.appendChild(
    element(
      "span",
      "detail",
      (model.authors.length ? "By " + model.authors.join(", ") + " · " : "") +
        model.file_name,
    ),
  );
  about.appendChild(element("span", "detail", modelFacts(model)));
  about.appendChild(element("span", "detail", model.licence_summary[0]));
  row.appendChild(about);

  if (model.warnings.length) {
    row.appendChild(element("span", "state is-cloud", model.warnings.length + " to check"));
  }

  const preview = element("button", "ghost", "Preview");
  preview.type = "button";
  preview.addEventListener("click", () => previewModel(model.sha256));
  row.appendChild(preview);

  return row;
}

function fillList(id, items) {
  const list = $(id);
  list.replaceChildren();
  items.forEach((text) => list.appendChild(element("li", "", text)));
}

function previewModel(sha256) {
  const model = avatar.library.models.find((m) => m.sha256 === sha256);
  if (!model) return;
  previewed = model;

  $("library-preview").hidden = false;
  $("preview-name").textContent = model.name;
  fillList("preview-terms", model.licence_summary);
  fillList("preview-warnings", model.warnings);
  $("preview-facts").textContent =
    modelFacts(model) +
    (model.authors.length ? " · by " + model.authors.join(", ") : "") +
    (model.licence.licence_url ? " · terms: " + model.licence.licence_url : "");

  $("preview-confirm-row").hidden = model.use.allowed;
  $("preview-confirm").checked = false;
  $("preview-confirm-text").textContent = model.use.confirmation;
  if (!$("preview-save-name").value) $("preview-save-name").value = model.name.slice(0, 40);

  showTester(null);

  const status = $("preview-status");
  const view = window.freyaAvatarView;

  if (!view) {
    status.textContent = "The 3D preview is not available in this window.";
    return;
  }
  if (!model.can_preview) {
    view.clear();
    status.textContent = "This model is too large to preview here.";
    return;
  }

  status.textContent = "Loading the model…";
  view
    .show("/avatar-models/" + encodeURIComponent(model.sha256), (done) => {
      status.textContent = "Loading the model… " + Math.round(done * 100) + "%";
    })
    .then((loaded) => {
      if (loaded && previewed === model) {
        status.textContent = "Drag to turn her, scroll to zoom.";
        showTester(loaded);
      }
    })
    .catch((error) => {
      status.textContent = error.message;
    });
}

// -- trying a model's expressions ---------------------------------------
//
// One button per expression the loaded model has. A button switches its
// expression on at the chosen strength and off again; several can be on
// together. The names come from the model file, so they are only ever
// shown as text.

const testerOn = new Set();

// The standard expressions, in the order their buttons are shown:
// feelings, then eyes, then the mouth shapes used for speech.
const EXPRESSION_LABELS = {
  neutral: "Neutral",
  happy: "Happy",
  relaxed: "Relaxed",
  sad: "Sad",
  angry: "Angry",
  surprised: "Surprised",
  blink: "Blink",
  blinkLeft: "Wink left",
  blinkRight: "Wink right",
  lookUp: "Look up",
  lookDown: "Look down",
  lookLeft: "Look left",
  lookRight: "Look right",
  aa: "Mouth: ah",
  ih: "Mouth: ih",
  ou: "Mouth: oo",
  ee: "Mouth: eh",
  oh: "Mouth: oh",
};

// "mouthSmileLeft" reads better as "mouth smile left".
function plainName(name) {
  return String(name).replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/_/g, " ").toLowerCase();
}

function testerStrength() {
  return Number($("preview-strength").value) / 100;
}

function testerButton(name, label) {
  const button = element("button", "ghost", label);
  button.type = "button";
  button.setAttribute("aria-pressed", "false");
  button.addEventListener("click", () => {
    const view = window.freyaAvatarView;
    if (!view) return;

    const on = !testerOn.has(name);
    if (on) testerOn.add(name);
    else testerOn.delete(name);

    button.classList.toggle("is-on", on);
    button.setAttribute("aria-pressed", String(on));
    view.setExpression(name, on ? testerStrength() : 0);
  });
  return button;
}

function showTester(loaded) {
  testerOn.clear();
  const main = $("preview-expressions");
  const shapes = $("preview-shapes");
  main.replaceChildren();
  shapes.replaceChildren();

  const order = Object.keys(EXPRESSION_LABELS);
  const place = (name) => (order.indexOf(name) + 1) || order.length + 1;
  const standard = ((loaded && loaded.standard) || [])
    .slice()
    .sort((a, b) => place(a) - place(b));
  const extra = (loaded && loaded.extra) || [];

  standard.forEach((name) => {
    main.appendChild(testerButton(name, EXPRESSION_LABELS[name] || plainName(name)));
  });
  extra.forEach((name) => shapes.appendChild(testerButton(name, plainName(name))));

  $("preview-shapes-box").hidden = extra.length === 0;
  $("preview-shapes-title").textContent =
    "Individual face shapes (" + extra.length + ")";
  $("preview-expression-note").textContent = !loaded
    ? ""
    : standard.length + extra.length === 0
      ? "This model has no expressions."
      : "Click one to switch it on and again to switch it off. Several can be on together.";
}

function resetTester() {
  testerOn.clear();
  document.querySelectorAll(".tester button.is-on").forEach((button) => {
    button.classList.remove("is-on");
    button.setAttribute("aria-pressed", "false");
  });
  if (window.freyaAvatarView) window.freyaAvatarView.resetExpressions();
}

// -- finding files that could become avatars -----------------------------

const SOURCE_STATES = {
  ready: ["Ready", "is-ready"],
  archive: ["Needs converting", "is-cloud"],
  convertible: ["Needs converting", "is-cloud"],
  unsuitable: ["Cannot be used", ""],
};

function sourceRow(source, creator) {
  const row = element("li");
  const about = element("div", "about");
  about.appendChild(element("span", "name", source.name));
  about.appendChild(
    element("span", "detail", source.megabytes + " MB · " + source.folder),
  );
  about.appendChild(element("span", "detail", source.note));
  if (source.inside && source.inside.length) {
    about.appendChild(element("span", "detail", "Inside: " + source.inside.join(", ")));
  }
  row.appendChild(about);

  const [label, style] = SOURCE_STATES[source.status] || ["", ""];
  row.appendChild(element("span", "state " + style, label));

  if (source.status !== "unsuitable") {
    const needsBlender = source.status !== "ready";
    const button = element(
      "button",
      "ghost",
      source.status === "ready" ? "Add" : "Make ready",
    );
    button.type = "button";
    button.disabled =
      Boolean(creator.job && creator.job.running) || (needsBlender && !creator.blender);
    if (needsBlender && !creator.blender) {
      button.title = "Converting needs Blender, which is not installed.";
    }
    button.addEventListener("click", () => {
      avatarSend(
        "prepare",
        { value: { source: source.id, author: $("creator-author").value } },
        "Starting…",
      );
    });
    row.appendChild(button);
  }

  return row;
}

function showCreator() {
  const creator = avatar.creator;
  if (!creator) return;

  $("creator-folders").textContent = creator.folders.join(", ");

  const list = $("creator-sources");
  list.replaceChildren();
  const sources = creator.sources;
  if (sources) {
    sources.forEach((source) => list.appendChild(sourceRow(source, creator)));
    if (!sources.length) list.appendChild(element("li", "", "Nothing found there."));
    $("creator-count").textContent =
      sources.length + (sources.length === 1 ? " file" : " files");
  }

  const job = creator.job || {};
  const line = $("creator-job");
  line.classList.toggle("is-error", Boolean(job.error));
  if (job.running) {
    line.textContent = job.name + ": " + job.progress;
  } else if (job.error) {
    line.textContent = job.name + ": " + job.error;
  } else if (job.file) {
    line.textContent =
      job.name + " is ready as " + job.file + ". It is in the list below: preview it there.";
  } else {
    line.textContent = "";
  }
  fillList("creator-notes", job.running || job.error ? [] : job.notes || []);
}

function avatarRow(saved) {
  const row = element("li");
  const about = element("div", "about");
  about.appendChild(element("span", "name", saved.name));
  about.appendChild(
    element(
      "span",
      "detail",
      "Model: " + saved.model_name +
        (saved.model_present ? "" : " · its file is no longer in the folder") +
        (saved.licence_confirmed ? " · you confirmed you may use it" : ""),
    ),
  );
  row.appendChild(about);

  const look = element("button", "ghost", "Preview");
  look.type = "button";
  look.disabled = !saved.model_present;
  look.addEventListener("click", () => {
    previewModel(saved.model);
    $("library-preview").scrollIntoView({ block: "nearest" });
  });
  row.appendChild(look);

  const isActive = avatar.library.active_profile === saved.id;
  const use = element(
    "button",
    isActive ? "primary" : "ghost",
    isActive ? "Freya's avatar" : "Use for Freya",
  );
  use.type = "button";
  use.disabled = !saved.model_present;
  use.title = isActive
    ? "Click to go back to the placeholder figure."
    : "Mark this as Freya's avatar.";
  use.addEventListener("click", () => {
    avatarSend("library-use", { value: { avatar: isActive ? null : saved.id } });
  });
  row.appendChild(use);

  const remove = element("button", "ghost", "Delete");
  remove.type = "button";
  remove.title = "Deletes the saved avatar, not the model file.";
  remove.addEventListener("click", () => {
    avatarSend("library-delete", { value: { avatar: saved.id } });
  });
  row.appendChild(remove);

  return row;
}

function showLibrary() {
  const library = avatar.library;
  if (!library) return;

  showCreator();

  $("library-folder").textContent = library.folder;
  $("library-count").textContent = libraryScanned || library.models.length
    ? library.models.length + (library.models.length === 1 ? " model" : " models")
    : "";

  const models = $("library-models");
  models.replaceChildren();
  library.models.forEach((model) => models.appendChild(modelRow(model)));

  fillList(
    "library-errors",
    library.errors.map((problem) => problem.file + ": " + problem.error),
  );

  const saved = $("library-avatars");
  saved.replaceChildren();
  library.profiles.forEach((profile) => saved.appendChild(avatarRow(profile)));
  if (!library.profiles.length) {
    saved.appendChild(element("li", "", "No saved avatars yet."));
  }

  // The model being previewed may have gone from the folder.
  if (previewed && !library.models.some((m) => m.sha256 === previewed.sha256)) {
    previewed = null;
    $("library-preview").hidden = true;
    if (window.freyaAvatarView) window.freyaAvatarView.clear();
  }
}

function scanLibrary() {
  libraryScanned = true;
  avatarSend("library-scan", {}, "Looking for models…");
}

function wireLibrary() {
  $("library-scan").addEventListener("click", scanLibrary);

  // Look once, the first time the Avatar tab is opened.
  $("tab-avatar").addEventListener("click", () => {
    if (!libraryScanned) scanLibrary();
  });

  $("creator-search").addEventListener("click", () => {
    avatarSend("search", {}, "Searching…");
  });

  $("preview-strength").addEventListener("input", () => {
    $("preview-strength-value").textContent = $("preview-strength").value + "%";
    const view = window.freyaAvatarView;
    if (view) testerOn.forEach((name) => view.setExpression(name, testerStrength()));
  });
  $("preview-reset").addEventListener("click", resetTester);
  $("preview-view-body").addEventListener("click", () => {
    if (window.freyaAvatarView) window.freyaAvatarView.setView("body");
  });
  $("preview-view-face").addEventListener("click", () => {
    if (window.freyaAvatarView) window.freyaAvatarView.setView("face");
  });

  $("preview-save").addEventListener("click", () => {
    if (!previewed) return;
    avatarSend(
      "library-save",
      {
        value: {
          name: $("preview-save-name").value,
          model: previewed.sha256,
          confirmed: $("preview-confirm").checked,
        },
      },
      "Saving…",
    );
  });
}

function avatarDisplay() {
  return (avatar.displays || []).find((d) => d.id === $("avatar-display").value);
}

function showAvatarMap() {
  const display = avatarDisplay();
  if (!display) return;
  $("avatar-map").style.aspectRatio =
    (display.work_area.right - display.work_area.left) + " / " +
    (display.work_area.bottom - display.work_area.top);
}

function mapPoint(event) {
  const box = $("avatar-map").getBoundingClientRect();
  return {
    x: Math.min(Math.max((event.clientX - box.left) / box.width, 0), 1),
    y: Math.min(Math.max((event.clientY - box.top) / box.height, 0), 1),
  };
}

function mapArea(a, b) {
  return [
    Math.min(a.x, b.x), Math.min(a.y, b.y),
    Math.max(a.x, b.x), Math.max(a.y, b.y),
  ];
}

function drawMapChoice(area) {
  const choice = $("avatar-map-choice");
  choice.hidden = false;
  choice.style.left = area[0] * 100 + "%";
  choice.style.top = area[1] * 100 + "%";
  choice.style.width = (area[2] - area[0]) * 100 + "%";
  choice.style.height = (area[3] - area[1]) * 100 + "%";
}

function avatarConfigure(changes, working) {
  avatarSend("configure", { value: changes }, working || "Saving…");
}

function wireAvatar() {
  $("avatar-show").addEventListener("click", () => avatarSend("show", {}, "Starting the avatar…"));
  $("avatar-hide").addEventListener("click", () => avatarSend("hide", {}));
  $("avatar-stop").addEventListener("click", () => avatarSend("stop", {}, "Closing…"));
  $("avatar-power").addEventListener("click", () => {
    const off = avatar && avatar.config && avatar.config.enabled === false;
    avatarSend(off ? "enable" : "disable", {}, off ? "Turning on…" : "Turning off…");
  });
  $("avatar-wave").addEventListener("click", () => avatarSend("wave", {}));
  $("avatar-idle").addEventListener("click", () => avatarSend("idle", {}));
  $("avatar-rest").addEventListener("click", () => avatarSend("rest", {}));

  $("avatar-appearance").addEventListener("change", () => {
    avatarSend("appearance", { value: $("avatar-appearance").value });
  });
  $("avatar-expression").addEventListener("change", () => {
    avatarSend("expression", { value: $("avatar-expression").value });
  });
  $("avatar-display").addEventListener("change", showAvatarMap);

  $("avatar-point").addEventListener("click", () => {
    avatarSend(
      "point",
      { display: $("avatar-display").value, target: $("avatar-target").value },
      "Pointing…",
    );
  });

  // Sliders show their value while dragged and are sent when let go.
  const label = (id) => {
    $(id).addEventListener("input", () => {
      $(id + "-value").textContent = Math.round(Number($(id).value)) + "%";
    });
  };

  label("avatar-size");
  $("avatar-size").addEventListener("change", () => {
    avatarSend("resize", { value: Number($("avatar-size").value) }, "Resizing…");
  });

  ["avatar-min", "avatar-max", "avatar-preferred"].forEach((id) => {
    label(id);
    $(id).addEventListener("change", () => {
      avatarConfigure({
        size: {
          minimum: Number($("avatar-min").value),
          maximum: Number($("avatar-max").value),
          preferred: Number($("avatar-preferred").value),
        },
      });
    });
  });

  Object.entries(AVATAR_CHECKS).forEach(([id, key]) => {
    $(id).addEventListener("change", () => {
      avatarConfigure({ [key]: $(id).checked });
    });
  });

  AVATAR_BOUNDS.forEach((edge) => {
    const id = "avatar-bound-" + edge;
    label(id);
    $(id).addEventListener("change", () => {
      const at = (name) => Number($("avatar-bound-" + name).value) / 100;
      avatarConfigure({
        boundary: {
          left: at("left"), top: at("top"), right: at("right"), bottom: at("bottom"),
        },
      });
    });
  });

  const map = $("avatar-map");
  map.addEventListener("pointerdown", (event) => {
    avatarMapStart = mapPoint(event);
    map.setPointerCapture(event.pointerId);
  });
  map.addEventListener("pointermove", (event) => {
    if (avatarMapStart) drawMapChoice(mapArea(avatarMapStart, mapPoint(event)));
  });
  map.addEventListener("pointerup", (event) => {
    if (!avatarMapStart) return;
    const area = mapArea(avatarMapStart, mapPoint(event));
    avatarMapStart = null;
    if (area[2] - area[0] < 0.01 || area[3] - area[1] < 0.01) {
      avatarMessage("Drag out a small box to choose a place.", true);
      return;
    }
    drawMapChoice(area);
    avatarSend(
      "point",
      { region: { display_id: $("avatar-display").value, area } },
      "Pointing…",
    );
  });
}

// -- images ----------------------------------------------------------

function addImage(image, newest) {
  // An image listed in the hello may also arrive as a live event
  // when it was finished just as the window joined.
  if (gallery.querySelector(`[data-image-id="${CSS.escape(image.id)}"]`)) return;
  $("gallery-empty").hidden = true;

  const card = element("figure", "card");
  card.dataset.imageId = image.id;
  const picture = element("img");
  picture.src = "/images/" + encodeURIComponent(image.id);
  picture.alt = image.prompt;
  picture.loading = "lazy";
  picture.addEventListener("click", () => openLightbox(image));

  const caption = element("figcaption", "", image.prompt);
  caption.appendChild(element("span", "when", when(image.created)));

  card.appendChild(picture);
  card.appendChild(caption);

  if (newest) {
    gallery.insertBefore(card, gallery.firstChild);
  } else {
    gallery.appendChild(card);
  }
}

// The same image, shown in the conversation where it was asked for.
function addChatImage(image) {
  const bubble = element("div", "message from-freya has-image");
  bubble.appendChild(element("span", "who", "Freya"));

  // The picture changes the height of the chat once it arrives, so
  // decide now whether the chat should stay at the bottom then.
  const stick = replaying || nearBottom(messages);

  const picture = element("img");
  picture.src = "/images/" + encodeURIComponent(image.id);
  picture.alt = image.prompt;
  picture.addEventListener("click", () => openLightbox(image));
  picture.addEventListener("load", () => {
    if (stick) messages.scrollTop = messages.scrollHeight;
  });

  bubble.appendChild(picture);
  bubble.appendChild(element("span", "caption", image.prompt));
  addToChat(bubble);
}

function openLightbox(image) {
  $("lightbox-image").src = "/images/" + encodeURIComponent(image.id);
  $("lightbox-image").alt = image.prompt;
  $("lightbox-caption").textContent = image.prompt;
  $("lightbox").hidden = false;
}

function closeLightbox() {
  $("lightbox").hidden = true;
  $("lightbox-image").removeAttribute("src");
}

// -- status ----------------------------------------------------------

function setPill(id, text, level) {
  const pill = $(id);
  pill.textContent = text;
  pill.classList.remove("is-good", "is-warn", "is-bad");
  if (level) pill.classList.add("is-" + level);
}

function showStatus(next) {
  status = next || {};

  const mic = {
    listening: ["Listening", "good"],
    muted: ["Muted", "warn"],
    off: ["Microphone off", "bad"],
  }[status.microphone] || ["Microphone", ""];
  setPill("pill-mic", mic[0], mic[1]);
  $("pill-mic").disabled = status.microphone === "off";
  $("pill-mic").title =
    status.microphone === "listening"
      ? "Listening for " + (status.names || []).join(", ") + ". Click to mute."
      : status.microphone === "muted"
        ? "Click to unmute."
        : "The microphone is not running.";

  setPill(
    "pill-model",
    status.model === "healthy" ? "Model ready" : "Model unavailable",
    status.model === "healthy" ? "good" : "bad",
  );

  setPill(
    "pill-voice",
    status.voice === "ready" ? "Voice ready" : "Voice unavailable",
    status.voice === "ready" ? "good" : "warn",
  );

  const images = {
    healthy: ["Images ready", "good"],
    startable: ["Images on demand", "warn"],
    unavailable: ["Images unavailable", "bad"],
  }[status.images] || ["Images", ""];
  setPill("pill-images", images[0], images[1]);

  // Leave the slider alone while it is being dragged.
  const volume = $("voice-volume");
  if (typeof status.volume === "number" && document.activeElement !== volume) {
    volume.value = status.volume;
    $("voice-volume-value").textContent = status.volume + "%";
  }
}

// -- connection --------------------------------------------------------

function connect() {
  socket = new WebSocket("ws://" + location.host + "/ws");

  socket.addEventListener("open", () => {
    banner.hidden = true;
    retryDelay = 500;
  });

  socket.addEventListener("message", (message) => {
    let event;
    try {
      event = JSON.parse(message.data);
    } catch (error) {
      return;
    }

    if (event.type === "hello") {
      messages.querySelectorAll(".message, .notice").forEach((n) => n.remove());
      gallery.querySelectorAll(".card").forEach((n) => n.remove());
      $("chat-empty").hidden = false;
      $("gallery-empty").hidden = false;
      currentReply = null;
      consoleLines = [];
      replaying = true;
      event.history.forEach(showEvent);
      replaying = false;
      finishReply();
      event.images.forEach((image) => addImage(image, false));
      addConsoleLines(event.console || []);
      showVoice(event.voice);
      showAvatar(event.avatar);
      showStatus(event.status);
      window.dispatchEvent(new Event("freya-connected"));
      messages.scrollTop = messages.scrollHeight;
      consoleOutput.scrollTop = consoleOutput.scrollHeight;
      return;
    }

    showEvent(event);
  });

  socket.addEventListener("close", () => {
    banner.hidden = false;
    finishReply();
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 5000);
  });
}

// -- controls ----------------------------------------------------------

function autoGrow(box) {
  box.style.height = "auto";
  box.style.height = Math.min(box.scrollHeight, 180) + "px";
}

function wireComposer(formId, inputId, deliver) {
  const form = $(formId);
  const input = $(inputId);

  const submit = () => {
    const text = input.value.trim();
    if (!text) return;
    if (deliver(text)) {
      input.value = "";
      autoGrow(input);
    }
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit();
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  });

  input.addEventListener("input", () => autoGrow(input));
}

const VIEW_INPUT = {
  chat: "chat-input",
  images: "image-input",
  voice: "preset-language",
  avatar: "avatar-appearance",
  console: "console-input",
};

function showView(name) {
  document.querySelectorAll(".tab").forEach((tab) => {
    const active = tab.dataset.view === name;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".view").forEach((view) => {
    view.classList.toggle("is-active", view.id === "view-" + name);
  });
  if (name === "images") $("images-badge").hidden = true;
  window.dispatchEvent(new CustomEvent("freya-view", {detail: name}));
  const input = $(VIEW_INPUT[name] || (name === "memory" ? "memory-query" : ""));
  if (input) input.focus();
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => showView(tab.dataset.view));
});

wireComposer("chat-form", "chat-input", submitText);
wireComposer("console-form", "console-input", submitText);
wireComposer("image-form", "image-input", (text) => send({ type: "image", text }));

$("stop-speaking").addEventListener("click", () => send({ type: "interrupt" }));

// The label follows the slider as it moves; Freya is told when it is let go.
$("voice-volume").addEventListener("input", () => {
  $("voice-volume-value").textContent = $("voice-volume").value + "%";
});
$("voice-volume").addEventListener("change", () => {
  send({ type: "volume", value: Number($("voice-volume").value) });
  $("voice-volume").blur();
});

$("console-hide").addEventListener("click", () => showConsoleTab(false));

$("pill-mic").addEventListener("click", () => {
  send({ type: "mute", muted: status.microphone === "listening" });
});

$("lightbox").addEventListener("click", closeLightbox);

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeLightbox();
});

if (recall(CONSOLE_SHOWN_KEY)) $("tab-console").hidden = false;

wireVoice();
wireAvatar();
wireLibrary();
wireMemory();
wireCloneAndDesign();
wireImport();

connect();
$("chat-input").focus();
