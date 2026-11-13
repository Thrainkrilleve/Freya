"use strict";

// The Cloud tab: which cloud models Freya may use, what may be sent to
// them, and the owner's own record of what was sent. Everything shown
// here is placed on the page as text, never as markup.

(() => {
  const byId = (id) => document.getElementById("cloud-" + id);
  let requests = 0;
  let loaded = false;

  const LISTS = [
    ["not_private_phrases", "not-private", "phrase"],
    ["not_private_memories", "not-private", "memory"],
    ["always_private_phrases", "always-private", "phrase"],
    ["always_private_memories", "always-private", "memory"],
  ];

  function node(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  }

  function say(text, isError) {
    const line = byId("message");
    line.textContent = text;
    line.classList.toggle("is-error", Boolean(isError));
  }

  function ask(action, fields, working) {
    requests += 1;
    if (send({type: "cloud", action, request: "c" + requests, ...(fields || {})})) {
      say(working === undefined ? "Saving…" : working);
    } else {
      say("Not connected to Freya.", true);
    }
  }

  function showProviders(providers) {
    const box = byId("providers");
    box.replaceChildren();
    providers.forEach((provider) => {
      const label = node("label", "check");
      const input = node("input");
      input.type = "checkbox";
      input.checked = provider.enabled;
      input.addEventListener("change", () => {
        ask("provider", {name: provider.name, enabled: input.checked});
      });
      label.append(input, node("span", "", provider.label));
      if (!provider.available) {
        label.append(node("span", "tag", "not set up yet"));
      }
      box.append(label);
      if (provider.weekly_limit !== null && provider.weekly_limit !== undefined) {
        const form = node("form", "actions");
        const field = node("label", "", "Weekly allowance (tokens, Freya's estimate) ");
        const amount = node("input");
        amount.type = "number";
        amount.min = "0";
        amount.step = "1000";
        amount.value = provider.weekly_limit;
        field.append(amount);
        const save = node("button", "ghost", "Save");
        save.type = "submit";
        form.append(field, save);
        form.addEventListener("submit", (event) => {
          event.preventDefault();
          ask("limit", {name: provider.name, weekly: Math.round(Number(amount.value))});
        });
        box.append(form);
        box.append(node(
          "p", "panel-note",
          "Used so far: " + (Number(provider.used) || 0).toLocaleString() +
            " of " + Number(provider.weekly_limit).toLocaleString() +
            ". Resets " + String(provider.resets_at || "").slice(0, 16).replace("T", " ") + " UTC.",
        ));
      }
    });
  }

  function showMaterial(material, settings) {
    const names = {allowed: "Allowed", ask: "Ask me first", never: "Never"};
    const box = byId("material");
    box.replaceChildren();
    material.forEach((entry) => {
      const label = node("label", "", entry.label);
      const select = node("select");
      settings.forEach((setting) => {
        const option = node("option", "", names[setting] || setting);
        option.value = setting;
        option.selected = setting === entry.setting;
        select.append(option);
      });
      select.addEventListener("change", () => {
        ask("material", {kind: entry.kind, setting: select.value});
      });
      label.append(select);
      box.append(label);
    });
  }

  function showBars(bars) {
    ["low", "normal", "high"].forEach((value) => {
      byId("bar-" + value).value = Math.round(Number(bars[value]) * 100);
    });
  }

  function showCorrections(corrections) {
    const boxes = {
      "not-private": byId("not-private"),
      "always-private": byId("always-private"),
    };
    Object.values(boxes).forEach((box) => box.replaceChildren());
    LISTS.forEach(([name, list, what]) => {
      (corrections[name] || []).forEach((entry) => {
        const row = node("li");
        const about = node("div", "about");
        about.append(node(
          "span", "said", what === "memory" ? "Memory " + entry : entry,
        ));
        const remove = node("button", "ghost", "Remove");
        remove.type = "button";
        remove.addEventListener("click", () => {
          ask("unmark", {list: name, entry});
        });
        row.append(about, remove);
        boxes[list].append(row);
      });
    });
    Object.values(boxes).forEach((box) => {
      if (!box.children.length) box.append(node("li", "panel-note", "Nothing here."));
    });
  }

  function showRules(rules) {
    const box = byId("rules");
    box.replaceChildren();
    if (!rules.length) {
      box.append(node("li", "panel-note", "Nothing here."));
      return;
    }
    rules.forEach((rule) => {
      const row = node("li");
      const about = node("div", "about");
      about.append(node(
        "span", "said",
        rule.task + " tasks may send " + (rule.material || []).join(", ") +
          " to " + rule.provider,
      ));
      about.append(node(
        "span", "panel-note", String(rule.made || "").slice(0, 10),
      ));
      const remove = node("button", "ghost", "Remove");
      remove.type = "button";
      remove.addEventListener("click", () => {
        ask("unrule", {id: rule.id});
      });
      row.append(about, remove);
      box.append(row);
    });
  }

  function showSent(sent) {
    const box = byId("sent");
    box.replaceChildren();
    if (!sent.length) {
      box.append(node("li", "panel-note", "Nothing has been sent."));
      return;
    }
    sent.forEach((entry) => {
      const row = node("li");
      const about = node("div", "about");
      about.append(node(
        "span", "panel-note",
        String(entry.at || "").slice(0, 16).replace("T", " ") +
          " · " + entry.provider + " · " + entry.outcome,
      ));
      about.append(node("pre", "said", entry.text));
      row.append(about);
      box.append(row);
    });
  }

  function show(state) {
    showProviders(state.providers || []);
    showMaterial(state.material || [], state.settings || []);
    showBars(state.bars || {});
    showCorrections(state.corrections || {});
    showRules(Array.isArray(state.rules) ? state.rules : []);
    const count = Number(state.sent_count) || 0;
    byId("sent-count").textContent =
      count === 1 ? "1 brief" : count + " briefs";
  }

  window.addEventListener("freya-cloud", (event) => {
    const answer = event.detail || {};
    if (answer.state) show(answer.state);
    if (Array.isArray(answer.sent)) showSent(answer.sent);
    say(answer.error || "", Boolean(answer.error));
  });

  window.addEventListener("freya-view", (event) => {
    // Asked for on every visit, not only the first: an answer to one
    // of Freya's questions can change these settings from outside
    // this tab, and a stale switch would then do the opposite of what
    // it shows.
    if (event.detail === "cloud") {
      ask("state", {}, loaded ? "" : "Loading…");
      loaded = true;
    }
  });

  // -- questions Freya puts to the owner ------------------------------
  // A brief's text is shown only here, only as text, and only in the
  // window that was asked. Nothing is kept once the question is closed.

  const dialog = document.getElementById("cloud-prompt");
  let asking = null;

  const TITLES = {
    approval: (who) => "Send some of this to " + who + "?",
    confirmation: (who) => "Send your spoken request to " + who + "?",
    hold: () => "This request matches something private",
    limit: () => "This would go over a usage limit",
    setting: () => "Change this cloud setting?",
  };

  function closePrompt() {
    asking = null;
    if (dialog.open) dialog.close();
    document.getElementById("cloud-prompt-text").textContent = "";
    document.getElementById("cloud-prompt-matches").replaceChildren();
    document.getElementById("cloud-prompt-choices").replaceChildren();
  }

  function answerPrompt(choice) {
    if (!asking) return;
    send({type: "cloud.answer", id: asking.id, choice});
    closePrompt();
    // The answer may have changed a setting or made a rule. If the
    // tab has been opened, bring it up to date once that is done.
    if (loaded) window.setTimeout(() => ask("state", {}, ""), 1500);
  }

  function showPrompt(prompt) {
    if (typeof prompt.id !== "string" || !Array.isArray(prompt.choices)) return;
    if (asking) answerPrompt(asking.fallback);
    const choices = prompt.choices.filter(
      (choice) => choice && typeof choice.id === "string",
    );
    if (!choices.length) return;

    // The last answer offered is always the one that sends nothing.
    asking = {id: prompt.id, fallback: choices[choices.length - 1].id};

    const title = (TITLES[prompt.kind] || (() => "Freya has a question"))(
      String(prompt.provider || "an outside model"),
    );
    document.getElementById("cloud-prompt-title").textContent = title;

    const lines = document.getElementById("cloud-prompt-lines");
    lines.replaceChildren();
    (prompt.lines || []).forEach((line) => lines.append(node("li", "", String(line))));

    document.getElementById("cloud-prompt-reason").textContent =
      prompt.reason ? String(prompt.reason) : "";

    const matches = document.getElementById("cloud-prompt-matches");
    matches.replaceChildren();
    (prompt.matches || []).forEach((match) => {
      matches.append(node("li", "", String(match.how) + ": " + String(match.text)));
    });

    document.getElementById("cloud-prompt-text-label").textContent =
      String(prompt.text_label || "The request");
    document.getElementById("cloud-prompt-text").textContent =
      typeof prompt.text === "string" ? prompt.text : "";

    const box = document.getElementById("cloud-prompt-choices");
    box.replaceChildren();
    choices.forEach((choice, position) => {
      const button = node(
        "button",
        position === choices.length - 1 && choices.length > 1 ? "ghost" : "primary",
        String(choice.label || choice.id),
      );
      button.type = "button";
      button.addEventListener("click", () => answerPrompt(choice.id));
      box.append(button);
    });

    if (!dialog.open) dialog.showModal();
  }

  // Closing the box with Escape is an answer, and it is "no".
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (asking) answerPrompt(asking.fallback);
  });

  window.addEventListener("freya-cloud-prompt", (event) => {
    showPrompt(event.detail || {});
  });

  window.addEventListener("freya-cloud-prompt-closed", (event) => {
    if (asking && event.detail && event.detail.id === asking.id) closePrompt();
  });

  // A new connection means the question is no longer waiting.
  window.addEventListener("freya-connected", closePrompt);

  byId("bars-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const bars = {};
    ["low", "normal", "high"].forEach((value) => {
      bars[value] = Number(byId("bar-" + value).value) / 100;
    });
    ask("bars", {bars});
  });

  byId("mark-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = byId("mark-value").value.trim();
    if (!value) return;
    const fields = {list: byId("mark-list").value};
    if (byId("mark-what").value === "memory") fields.memory = value;
    else fields.phrase = value;
    ask("mark", fields);
    byId("mark-value").value = "";
  });

  byId("sent-show").addEventListener("click", () => {
    ask("sent", {}, "Loading…");
  });
})();
