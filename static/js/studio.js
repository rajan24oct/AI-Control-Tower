/* AI Solution Studio: blueprint page helpers and the sandbox composer. */
(function () {
  "use strict";

  function csrf() {
    var input = document.querySelector("input[name=csrfmiddlewaretoken]");
    if (input) return input.value;
    var match = document.cookie.match(/(^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[2]) : "";
  }

  function busy(button, on) {
    if (!button) return;
    button.disabled = on;
    var idle = button.querySelector(".ct-idle"), working = button.querySelector(".ct-busy");
    if (idle && working) {
      idle.classList.toggle("d-none", on);
      working.classList.toggle("d-none", !on);
    }
  }

  function post(url, data) {
    var body = new URLSearchParams(data);
    return fetch(url, {
      method: "POST", body: body, credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrf() }
    }).then(function (response) {
      return response.json().catch(function () {
        return { ok: false, message: "The server returned an unexpected response (" + response.status + ")." };
      });
    });
  }

  // -- Blueprint page ------------------------------------------------------------------------

  document.querySelectorAll("[data-studio-generate]").forEach(function (form) {
    form.addEventListener("submit", function () { busy(form.querySelector("button"), true); });
  });

  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var source = document.querySelector(button.getAttribute("data-copy"));
      if (!source || !navigator.clipboard) return;
      navigator.clipboard.writeText(source.innerText).then(function () {
        var label = button.innerHTML;
        button.innerHTML = '<i class="bi bi-check2"></i> Copied';
        setTimeout(function () { button.innerHTML = label; }, 1500);
      });
    });
  });

  // -- Sandbox ---------------------------------------------------------------------------------

  var composer = document.getElementById("sandbox-composer");
  if (!composer) return;
  var input = document.getElementById("sandbox-input");
  var runButton = document.getElementById("sandbox-run");
  var runAll = document.getElementById("sandbox-run-all");
  var results = document.getElementById("sandbox-results");
  var error = document.getElementById("sandbox-error");

  function showError(message) {
    error.textContent = message || "";
    error.classList.toggle("d-none", !message);
  }

  function run(text) {
    showError("");
    return post(composer.action, { input: text }).then(function (reply) {
      if (!reply.ok) { showError(reply.message); return false; }
      var empty = document.getElementById("sandbox-empty");
      if (empty) empty.remove();
      results.insertAdjacentHTML("afterbegin", reply.html);
      return true;
    }).catch(function () {
      showError("The sandbox could not be reached. Check your connection and try again.");
      return false;
    });
  }

  composer.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = input.value.trim();
    if (!text) { input.focus(); return; }
    busy(runButton, true);
    if (runAll) runAll.disabled = true;
    run(text).then(function (ok) {
      busy(runButton, false);
      if (runAll) runAll.disabled = false;
      if (ok) input.value = "";
    });
  });

  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      composer.requestSubmit ? composer.requestSubmit() : composer.dispatchEvent(new Event("submit"));
    }
  });

  document.querySelectorAll(".ct-sample").forEach(function (chip) {
    chip.addEventListener("click", function () {
      input.value = chip.getAttribute("data-sample");
      input.focus();
    });
  });

  if (runAll) {
    runAll.addEventListener("click", function () {
      var samples = Array.prototype.map.call(document.querySelectorAll(".ct-sample"), function (chip) {
        return chip.getAttribute("data-sample");
      });
      var original = runAll.innerHTML;
      runAll.disabled = true;
      busy(runButton, true);
      var index = 0;
      (function next() {
        if (index >= samples.length) {
          runAll.innerHTML = original;
          runAll.disabled = false;
          busy(runButton, false);
          return;
        }
        runAll.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Sample ' + (index + 1) + " of " + samples.length;
        run(samples[index++]).then(next);
      })();
    });
  }

  results.addEventListener("click", function (event) {
    var button = event.target.closest("[data-rating]");
    if (!button) return;
    var group = button.closest("[data-rate-url]");
    var value = button.classList.contains("active") ? "" : button.getAttribute("data-rating");
    post(group.getAttribute("data-rate-url"), { rating: value }).then(function (reply) {
      if (!reply.ok) return;
      group.querySelectorAll("[data-rating]").forEach(function (b) {
        b.classList.toggle("active", String(reply.rating) === b.getAttribute("data-rating"));
      });
    });
  });
})();
