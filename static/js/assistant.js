/* ===========================================================================
   AI Control Tower - assistant widget
   ---------------------------------------------------------------------------
   A chat panel at the bottom right of every page. The conversation is kept in
   sessionStorage, so it survives moving between pages in the same tab and is
   gone when the tab closes. Answers arrive as HTML the server has already
   escaped and rendered; anything else -- sources, links, suggestions -- is
   built here with textContent, never parsed as markup.
   =========================================================================== */

(function () {
  "use strict";

  var root = document.querySelector("[data-assistant]");
  if (!root) return;

  var STORE = "ct-irgf-agent-v2";
  var panel = root.querySelector(".ct-assistant-panel");
  var fab = root.querySelector(".ct-assistant-fab");
  var log = root.querySelector("[data-assistant-log]");
  var form = root.querySelector("[data-assistant-form]");
  var input = root.querySelector("[data-assistant-input]");
  var welcome = root.querySelector("[data-assistant-welcome]");
  var askUrl = root.getAttribute("data-ask-url");
  var guideTemplate = root.querySelector("[data-assistant-guide-template]");
  var agentName = (root.querySelector("#ct-assistant-title") || {}).textContent || "IRGF Agent";
  var busy = false;

  function load() {
    try { return JSON.parse(sessionStorage.getItem(STORE)) || {open: false, messages: []}; }
    catch (e) { return {open: false, messages: []}; }
  }
  function save() {
    try { sessionStorage.setItem(STORE, JSON.stringify(state)); } catch (e) { /* private mode: fine */ }
  }
  var state = load();

  function csrf() {
    var match = document.cookie.match(/(^|;\s*)csrftoken=([^;]+)/);
    if (match) return decodeURIComponent(match[2]);
    var field = document.querySelector("[name=csrfmiddlewaretoken]");
    return field ? field.value : "";
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function scrollDown() { log.scrollTop = log.scrollHeight; }

  function chips(list) {
    var box = el("div", "ct-assistant-chips");
    list.forEach(function (text) {
      var chip = el("button", "", text);
      chip.type = "button";
      chip.setAttribute("data-assistant-suggest", "");
      box.appendChild(chip);
    });
    return box;
  }

  function guide() {
    return guideTemplate.content.firstElementChild.cloneNode(true);
  }

  function renderMessage(message) {
    var bubble = el("div", "ct-assistant-msg " + (message.role === "user" ? "is-user" : "is-bot"));
    if (message.role !== "user") bubble.appendChild(el("p", "ct-assistant-who", agentName));
    if (message.role === "guide") {
      bubble.appendChild(el("p", "", "Here are the guided questions. Pick one, or ask your own below."));
      bubble.appendChild(guide());
    } else if (message.role === "user") {
      bubble.appendChild(el("p", "", message.text));
    } else if (message.error) {
      bubble.classList.add("is-error");
      bubble.appendChild(el("p", "", message.error));
    } else {
      var body = el("div", "ct-assistant-answer");
      body.innerHTML = message.html; // server-rendered and escaped; see apps/assistant/knowledge.py
      bubble.appendChild(body);
      var modes = {
        chatgpt: "Answered by ChatGPT with the IRGF framework and Control Tower skills",
        live: "Live from your organization",
        rules: "From the rules the platform enforces"
      };
      if (modes[message.mode]) bubble.appendChild(el("p", "ct-assistant-mode", modes[message.mode]));
      if (message.sources && message.sources.length) {
        var sources = el("div", "ct-assistant-sources");
        sources.appendChild(el("span", "", "Sources"));
        message.sources.forEach(function (source) {
          var a = el("a", "", source.title);
          a.href = source.url;
          a.title = source.doc;
          sources.appendChild(a);
        });
        bubble.appendChild(sources);
      }
      if (message.links && message.links.length) {
        var links = el("div", "ct-assistant-links");
        message.links.forEach(function (link) {
          var a = el("a", "", link.label + " →");
          a.href = link.url;
          links.appendChild(a);
        });
        bubble.appendChild(links);
      }
      if (message.suggestions && message.suggestions.length) bubble.appendChild(chips(message.suggestions));
    }
    log.appendChild(bubble);
  }

  function renderAll() {
    Array.prototype.slice.call(log.querySelectorAll(".ct-assistant-msg:not([data-assistant-welcome])"))
      .forEach(function (node) { node.remove(); });
    state.messages.forEach(renderMessage);
    scrollDown();
  }

  function setOpen(open, focus) {
    state.open = open;
    save();
    panel.hidden = !open;
    fab.setAttribute("aria-expanded", open ? "true" : "false");
    root.classList.toggle("is-open", open);
    if (open) { scrollDown(); if (focus) input.focus(); }
  }

  function history() {
    // Everything before the question just added; guides and errors carry no conversation.
    return state.messages.slice(0, -1).filter(function (m) {
      return (m.role === "user" && m.text) || (m.role === "bot" && m.text);
    }).slice(-8).map(function (m) { return {role: m.role, text: m.text}; });
  }

  function ask(question) {
    question = (question || "").trim();
    if (!question || busy) return;
    busy = true;
    state.messages.push({role: "user", text: question});
    renderMessage(state.messages[state.messages.length - 1]);
    save();
    input.value = "";
    autosize();

    var typing = el("div", "ct-assistant-msg is-bot is-typing");
    typing.appendChild(el("span")); typing.appendChild(el("span")); typing.appendChild(el("span"));
    typing.setAttribute("aria-label", "Answering");
    log.appendChild(typing);
    scrollDown();

    fetch(askUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrf(), "X-Requested-With": "XMLHttpRequest"},
      // The last few turns as plain text, so a follow-up question has its context.
      body: JSON.stringify({question: question, history: history()})
    })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) {
            throw new Error(data.error || (response.status === 403
              ? "Your session has ended. Sign in again to keep asking."
              : "Something went wrong answering that."));
          }
          return data;
        });
      })
      .then(function (data) {
        state.messages.push({role: "bot", html: data.html, text: data.text, sources: data.sources,
                             links: data.links, suggestions: data.suggestions, mode: data.mode});
      })
      .catch(function (error) {
        state.messages.push({role: "bot", error: error.message || "The assistant could not be reached."});
      })
      .then(function () {
        typing.remove();
        // Keep the conversation to the last 40 messages; older ones are rarely re-read.
        state.messages = state.messages.slice(-40);
        renderMessage(state.messages[state.messages.length - 1]);
        save();
        scrollDown();
        busy = false;
        input.focus();
      });
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  }

  root.addEventListener("click", function (event) {
    if (event.target.closest("[data-assistant-guide]")) {
      state.messages.push({role: "guide"});
      renderMessage(state.messages[state.messages.length - 1]);
      save();
      scrollDown();
      return;
    }
    var toggle = event.target.closest("[data-assistant-toggle]");
    if (toggle) { setOpen(panel.hidden, true); return; }
    var suggest = event.target.closest("[data-assistant-suggest]");
    if (suggest) { ask(suggest.textContent); return; }
    if (event.target.closest("[data-assistant-clear]")) {
      state.messages = [];
      save();
      renderAll();
      welcome.hidden = false;
      input.focus();
    }
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    ask(input.value);
  });

  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      ask(input.value);
    }
  });
  input.addEventListener("input", autosize);

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !panel.hidden) {
      setOpen(false);
      fab.focus();
    }
  });

  root.querySelector("[data-assistant-guide-slot]").appendChild(guide());
  renderAll();
  setOpen(!!state.open);
})();
