"use strict";

(() => {
  const byId = (id) => document.getElementById("media-" + id);
  let selected = null;
  let player = null;
  let skips = [];
  let page = 0;
  let active = false;
  let request = 0;
  const pending = new Map();
  const node = (tag, text) => {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    return result;
  };
  const status = (text) => { byId("status").textContent = text; };
  function ask(action, fields = {}, done = null) {
    if (pending.size >= 100) { status("Too many requests are waiting."); return; }
    const id = "media-" + (++request);
    if (!send({type: "media", action, request: id, ...fields})) {
      status("Not connected to Freya."); return;
    }
    pending.set(id, {done, at: Date.now()});
  }
  function button(parent, label, handler) {
    const result = node("button", label);
    result.className = "ghost";
    result.type = "button";
    result.addEventListener("click", handler);
    parent.append(result);
    return result;
  }
  function link(parent, label, url, download = false) {
    let target;
    try { target = new URL(url, location.origin); } catch (_) { return; }
    if (!["http:", "https:"].includes(target.protocol) || target.username || target.password) return;
    const result = node("a", label);
    result.href = target.href;
    result.className = "ghost link-button";
    if (download) result.download = "";
    else { result.target = "_blank"; result.rel = "noopener noreferrer"; }
    parent.append(result);
  }
  function row(parent, text) {
    const result = node("div"); result.className = "media-row";
    result.append(node("div", text)); parent.append(result); return result;
  }
  function refresh() {
    ask("overview", {}, (answer) => {
      showLibrary(answer.items || []); showJobs(answer.jobs || []);
      byId("auto").checked = answer.settings?.analyze_played === true;
      const availability = answer.availability || {};
      byId("setup").textContent = (availability.roots || []).map((r) => r.path + (r.available ? "" : " (unavailable)")).join(" · ") || "Choose media folders in Freya's startup configuration. See the media setup guide.";
    });
  }
  function showLibrary(items) {
    const list = byId("library"); list.replaceChildren();
    if (!items.length) list.append(node("p", "No imported media yet. Find downloaded media or add a YouTube link."));
    items.forEach((item) => button(row(list, item.title + " · " + item.provider), "Open", () => open(item.id)));
  }
  function open(id) {
    if (!/^[0-9a-f]{32}$/.test(id)) return;
    ask("open", {media_id: id}, (answer) => {
      if (selected?.id !== id) clearPlayer();
      selected = answer.item; skips = answer.skips || [];
      byId("selected-title").textContent = selected.title;
      byId("tags").value = (selected.tags || []).join(", ");
      ["play", "analyze", "export", "delete"].forEach((key) => { byId(key).disabled = false; });
      byId("coverage").textContent = selected.coverage?.statement || "Not analyzed. Speech analysis requires a transcript or loaded speech recognizer; visual analysis is not available yet.";
      showNotes(answer.notes || []); showSkips(skips);
      const text = byId("text"); text.replaceChildren();
      (answer.summaries || []).forEach((s) => row(text, "Summary (inference), " + s.start + "–" + s.end + "s: " + s.text));
      (answer.transcript || []).forEach((s) => button(row(text, s.start + "s · " + s.text), "Seek", () => seek(s.start)));
      if (answer.transcript_total > (answer.transcript || []).length) text.append(node("p", "Showing the first 500 segments. Search or export to read the rest."));
    });
  }
  function currentTime() { return player ? player.currentTime : Number(byId("time").value); }
  function seek(seconds) {
    if (player && Number.isFinite(seconds)) player.currentTime = seconds;
    byId("time").value = seconds ?? 0;
    if (selected?.provider === "youtube" && byId("player").children.length && Number.isFinite(seconds)) play();
  }
  function clearPlayer() {
    if (player) { player.pause(); player.removeAttribute("src"); player.load(); }
    player = null; byId("player").replaceChildren();
  }
  function play() {
    if (!selected) return;
    ask("play", {media_id: selected.id, start: Number(byId("time").value)}, (answer) => {
      if (!selected || selected.id !== answer.media_id) return;
      clearPlayer(); skips = answer.skips || [];
      const source = answer.player;
      if (source.kind === "youtube") {
        const url = new URL(source.embed_url);
        if (url.origin !== "https://www.youtube-nocookie.com" || !/^\/embed\/[A-Za-z0-9_-]{11}$/.test(url.pathname)) return;
        const frame = node("iframe"); frame.src = url.href;
        frame.title = selected.title; frame.referrerPolicy = "strict-origin-when-cross-origin";
        frame.allow = "encrypted-media; fullscreen; picture-in-picture"; frame.allowFullscreen = true;
        byId("player").append(frame);
        status("YouTube player loaded. Enter timestamps manually for notes; supply an imported copy or transcript for analysis.");
      } else if (source.url) {
        if (!/^\/media\/stream\/[A-Za-z0-9_-]{20,64}$/.test(source.url)) return;
        player = node(source.media === "audio" ? "audio" : "video");
        player.controls = true; player.preload = "metadata"; player.src = source.url;
        player.addEventListener("loadedmetadata", () => {
          if (Number.isFinite(player.duration)) ask("duration", {media_id: selected.id, seconds: player.duration});
        });
        player.addEventListener("timeupdate", () => {
          const skip = skips.find((s) => s.confirmed && player.currentTime >= s.start && player.currentTime < s.end);
          if (skip) player.currentTime = skip.end;
        });
        player.addEventListener("error", () => status("The browser cannot play this file. Try Jellyfin's player or import a browser-supported format."));
        byId("player").append(player);
      }
      if (source.handoff_url) link(byId("player"), "Open in Jellyfin", source.handoff_url);
      if (source.note) byId("player").append(node("p", source.note));
      if (answer.auto_analysis) status("Automatic analysis: " + answer.auto_analysis);
    });
  }
  function showNotes(notes) {
    byId("notes").replaceChildren();
    notes.forEach((n) => {
      const item = row(byId("notes"), (n.at ?? "Untimed") + "s · " + n.text);
      button(item, "Seek", () => seek(n.at));
      button(item, "Remove note", () => ask("note-delete", {media_id: selected.id, id: n.id}, (a) => showNotes(a.notes)));
    });
  }
  function showSkips(items) {
    byId("skips").replaceChildren();
    items.forEach((s) => button(row(byId("skips"), s.start + "–" + s.end + "s · " + s.label), "Remove skip", () => ask("skip-delete", {media_id: selected.id, id: s.id}, (a) => { skips = a.skips; showSkips(skips); })));
  }
  function showJobs(jobs) {
    byId("jobs").replaceChildren();
    jobs.forEach((j) => {
      const item = row(byId("jobs"), (j.title || "Removed media") + " · " + j.state + " · " + (j.detail || j.error || j.stage || ""));
      if (["queued", "running"].includes(j.state)) button(item, "Cancel", () => ask("cancel", {job_id: j.job_id}, (a) => showJobs(a.jobs)));
    });
  }
  const on = (key, handler) => byId(key).addEventListener("click", handler);
  on("refresh", refresh);
  on("filter", () => ask("list", {tag: byId("tag-filter").value.trim()}, (a) => showLibrary(a.items)));
  on("scan", () => ask("scan", {}, (a) => {
    const list = byId("candidates"); list.replaceChildren();
    (a.files || a.candidates || []).forEach((f) => button(row(list, f.location || f.name), f.imported ? "Already imported" : "Import", () => ask("import", {candidates: [f.candidate]}, (answer) => { status((answer.problems || []).join(" · ") || "Imported."); refresh(); })));
    if (!(a.files || a.candidates || []).length) list.append(node("p", "No completed files found. Wait for downloads to finish and scan again."));
    if (a.truncated) list.append(node("p", "Scan limit reached. Choose smaller media folders."));
  }));
  on("bookmark", () => ask("bookmark", {url: byId("url").value, title: byId("title").value}, (a) => { refresh(); open(a.item.id); }));
  on("discover", () => ask("discover", {query: byId("web-query").value}, (a) => {
    a = a.discovery || {};
    const list = byId("discovery"); list.replaceChildren();
    if (a.external) link(list, a.external.label, a.external.url);
    (a.results || []).forEach((item) => button(row(list, item.title + " · " + item.channel), "Add link", () => ask("bookmark", {url: item.url, title: item.title, creator: item.channel}, () => refresh())));
  }));
  on("jellyfin-connect", () => ask("jellyfin-libraries", {}, (a) => {
    const list = byId("jellyfin-library"); list.replaceChildren();
    (a.libraries || []).forEach((item) => { const option = node("option", item.name); option.value = item.id; list.append(option); });
  }));
  function browse() {
    ask("jellyfin-items", {library: byId("jellyfin-library").value || null, query: byId("jellyfin-query").value, start: page}, (a) => {
      a = a.page || {};
      const list = byId("jellyfin-results"); list.replaceChildren();
      (a.items || []).forEach((item) => button(row(list, item.name), "Add to library", () => ask("jellyfin-add", {item: item.id}, (added) => { refresh(); open(added.item.id); })));
      byId("jellyfin-next").hidden = !a.items?.length || (a.total !== null && page + a.items.length >= a.total);
    });
  }
  on("jellyfin-search", () => { page = 0; browse(); });
  on("jellyfin-next", () => { page += 50; browse(); });
  on("play", play);
  on("stamp", () => { if (player) byId("time").value = currentTime().toFixed(1); else status("Enter the timestamp shown in YouTube's player."); });
  on("save-tags", () => { if (selected) ask("tags", {media_id: selected.id, tags: byId("tags").value.split(",").map((t) => t.trim()).filter(Boolean)}, refresh); });
  on("add-note", () => { if (selected) ask("note-add", {media_id: selected.id, at: Number(byId("time").value), text: byId("note").value}, (a) => { showNotes(a.notes); byId("note").value = ""; }); });
  on("add-skip", () => { if (selected && selected.provider !== "youtube") ask("skip-add", {media_id: selected.id, start: Number(byId("skip-start").value), end: Number(byId("skip-end").value), label: byId("skip-label").value, confirmed: true}, (a) => { skips = a.skips; showSkips(skips); }); else status("Skip intervals apply to imported media, not the YouTube player."); });
  on("analyze", () => { if (selected) ask("analyze", {media_id: selected.id}, (a) => showJobs(a.jobs)); });
  on("export", () => { if (selected) ask("export", {media_id: selected.id}, () => {
    byId("exports").replaceChildren();
    ["json", "md"].forEach((kind) => link(byId("exports"), "Save " + kind.toUpperCase(), "/media/export/" + selected.id + "/" + kind, true));
  }); });
  on("delete", () => {
    if (selected && window.confirm("Remove this catalog record, notes and derived memories? Your original file stays.")) ask("delete", {media_id: selected.id}, () => {
      selected = null; clearPlayer(); byId("selected-title").textContent = "Choose an item";
      ["notes", "skips", "text", "exports"].forEach((key) => byId(key).replaceChildren());
      ["play", "analyze", "export", "delete"].forEach((key) => { byId(key).disabled = true; }); refresh();
    });
  });
  on("import-transcript", async () => {
    if (!selected) return;
    const file = byId("transcript-file").files[0];
    if (file && file.size > 3 * 1024 * 1024) { status("Transcript files must be under 3 MB."); return; }
    const text = file ? await file.text() : byId("transcript-text").value;
    // The gateway's regular message limit is 64 KiB; use its bounded upload path for larger captions.
    if (new TextEncoder().encode(text).length > 50000) {
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      const bytes = new TextEncoder().encode(text);
      for (let offset = 0; offset < bytes.length; offset += 60000) socket.send(bytes.slice(offset, offset + 60000));
      ask("transcript-import", {media_id: selected.id, name: (file?.name || "pasted").slice(0, 80)}, () => open(selected.id));
    } else ask("transcript-import", {media_id: selected.id, name: (file?.name || "pasted").slice(0, 80), text}, () => open(selected.id));
  });
  on("memory-search", () => ask("search", {query: byId("memory-query").value}, (a) => {
    const list = byId("memory-results"); list.replaceChildren();
    (a.results || []).forEach((item) => button(row(list, item.title + " · " + item.start + "s · " + item.evidence + ": " + item.text), "Open source", () => { open(item.media_id); byId("time").value = item.start || 0; }));
    if (!a.results?.length) list.append(node("p", "No matching media memories."));
  }));
  byId("auto").addEventListener("change", () => ask("settings", {analyze_played: byId("auto").checked}));
  window.addEventListener("freya-media", (event) => {
    const answer = event.detail; const waiting = pending.get(answer.request); pending.delete(answer.request);
    if (answer.error) { status(answer.error); return; }
    status("Ready."); if (waiting?.done) waiting.done(answer);
  });
  window.addEventListener("freya-connected", () => { pending.clear(); if (active) refresh(); });
  window.addEventListener("freya-view", (event) => {
    active = event.detail === "media";
    if (active) refresh(); else clearPlayer();
  });
  document.addEventListener("visibilitychange", () => { if (document.hidden) clearPlayer(); });
  setInterval(() => {
    for (const [id, value] of pending) if (Date.now() - value.at > 30000) { pending.delete(id); status("A media request timed out. Refresh to check its outcome."); }
    if (active && pending.size === 0 && !document.hidden) ask("jobs", {}, (a) => showJobs(a.jobs || []));
  }, 5000);
})();
