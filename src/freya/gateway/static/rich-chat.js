"use strict";

// A small display-only Markdown subset. Provider text is never parsed as HTML.
(() => {
  function node(tag, text) {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    return result;
  }

  function safeURL(raw, allowLocal = false) {
    if (typeof raw !== "string" || raw.length > 4096 || /[\s\\]/.test(raw)) return null;
    if (allowLocal && /^\/(?:images|media\/stream)\/[A-Za-z0-9_-]{1,200}$/.test(raw)) return raw;
    let url;
    try { url = new URL(raw); } catch (_) { return null; }
    // A trailing dot names the same host to the browser.
    const host = url.hostname.toLowerCase().replace(/\.+$/, "");
    if (url.protocol !== "https:" || url.username || url.password || url.port) return null;
    if (!host.includes(".") || /(^|\.)(localhost|local|internal)$/.test(host)) return null;
    // Numeric hosts are not needed for answer context. Block alternate IP spellings too.
    if (/^[\d.]+$/.test(host) || host.includes(":")) return null;
    return url.href;
  }

  function youtubeID(url) {
    if (url.startsWith("/")) return null;
    const parsed = new URL(url);
    let id = null;
    if (parsed.hostname === "youtu.be") id = parsed.pathname.slice(1);
    else if (["youtube.com", "www.youtube.com", "m.youtube.com"].includes(parsed.hostname)) {
      if (parsed.pathname === "/watch") id = parsed.searchParams.get("v");
      else if (/^\/(embed|shorts)\//.test(parsed.pathname)) id = parsed.pathname.split("/")[2];
    }
    return id && /^[A-Za-z0-9_-]{11}$/.test(id) ? id : null;
  }

  function link(label, url) {
    const anchor = node("a", label);
    anchor.href = url;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    anchor.referrerPolicy = "no-referrer";
    return anchor;
  }

  function media(label, url, kind, videoID = null) {
    const figure = node("figure");
    figure.className = "answer-media";
    const caption = node("figcaption", label || (kind === "image" ? "Picture" : "Video"));
    const button = node("button", kind === "image" ? "Show picture" : "Play video");
    button.type = "button";
    button.className = "ghost";
    figure.append(caption, button, link("Open source", url));
    button.addEventListener("click", () => {
      let view;
      if (url.startsWith("/images/")) {
        view = node("img");
        view.alt = label;
        view.src = url;
      } else if (url.startsWith("/media/stream/")) {
        view = node("video");
        view.controls = true;
        view.preload = "none";
        view.playsInline = true;
        view.src = url;
      } else {
        view = node("iframe");
        view.title = label || "Answer media";
        view.referrerPolicy = "no-referrer";
        if (videoID) {
          view.setAttribute("sandbox", "allow-scripts allow-same-origin allow-presentation");
          view.allow = "fullscreen";
          view.src = "https://www.youtube-nocookie.com/embed/" + videoID;
        } else {
          view.setAttribute("sandbox", "");
          view.src = "/answer-media?" + new URLSearchParams({kind, url});
        }
      }
      figure.insertBefore(view, caption);
      button.remove();
    }, {once: true});
    return figure;
  }

  function inline(parent, text, budget) {
    // Link/image destinations with spaces or parentheses stay plain text.
    const pattern = /(!?\[([^\]\n]*)\]\(([^\s()]+)\)|`([^`\n]+)`|\*\*([^*\n]+)\*\*)/g;
    let cursor = 0;
    for (const match of text.matchAll(pattern)) {
      parent.append(document.createTextNode(text.slice(cursor, match.index)));
      if (match[4] !== undefined) parent.append(node("code", match[4]));
      else if (match[5] !== undefined) parent.append(node("strong", match[5]));
      else {
        const image = match[1].startsWith("!");
        const url = safeURL(match[3], true);
        if (!url) parent.append(document.createTextNode(match[0]));
        else if (image && budget.remaining > 0) {
          budget.remaining--;
          parent.append(media(match[2], url, "image"));
        }
        else {
          if (image) { parent.append(link(match[2], url)); cursor = match.index + match[0].length; continue; }
          const videoID = youtubeID(url);
          const video = url.startsWith("/media/stream/") || (!url.startsWith("/") && /\.(mp4|webm|ogg)$/i.test(new URL(url).pathname));
          if ((videoID || video) && budget.remaining > 0) {
            budget.remaining--;
            parent.append(media(match[2], url, "video", videoID));
          } else parent.append(link(match[2], url));
        }
      }
      cursor = match.index + match[0].length;
    }
    parent.append(document.createTextNode(text.slice(cursor)));
  }

  function render(container, value) {
    const text = typeof value === "string" ? value : "";
    container.replaceChildren();
    container.classList.add("rich-answer");
    let code = null;
    let list = null;
    const budget = {remaining: 12};
    for (const line of text.split("\n")) {
      if (line.startsWith("```")) {
        list = null;
        if (code) code = null;
        else { code = node("code"); const pre = node("pre"); pre.append(code); container.append(pre); }
        continue;
      }
      if (code) { code.append(document.createTextNode(line + "\n")); continue; }
      if (!line.trim()) { list = null; continue; }
      const bullet = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
      if (bullet) {
        if (!list) { list = node("ul"); container.append(list); }
        const item = node("li"); inline(item, bullet[1], budget); list.append(item);
        continue;
      }
      list = null;
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      const block = node(heading ? "h" + Math.min(heading[1].length + 1, 5) : "div");
      inline(block, heading ? heading[2] : line, budget);
      container.append(block);
    }
  }

  function sourceMessage(event, addToChat) {
    if (!event || typeof event.text !== "string" || !event.source || typeof event.source.name !== "string") return;
    const bubble = node("div");
    bubble.className = "message from-source";
    const who = node("span", event.source.name.slice(0, 80));
    who.className = "who";
    const body = node("div");
    body.className = "body";
    render(body, event.text);
    bubble.append(who, body);
    if (event.truncated) {
      const note = node("div", "This source response was shortened.");
      note.className = "caption";
      bubble.append(note);
    }
    addToChat(bubble);
    return bubble;
  }

  const api = {render, sourceMessage, safeURL};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else window.FreyaRichChat = api;
})();
