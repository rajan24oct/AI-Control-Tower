/* ===========================================================================
   AI Control Tower - AJAX layer (jQuery)
   ---------------------------------------------------------------------------
   Three behaviours, declared with data attributes rather than wired per page:

     data-ajax-filter   a form whose results replace a target region
     data-ajax-action   a form whose POST is applied without leaving the page
     data-ajax-poll     a region refreshed on an interval

   Every request degrades to the ordinary Django round trip if scripting is
   unavailable, because each one is a real form or link underneath.
   =========================================================================== */

(function ($) {
  "use strict";

  /* Chart palette, defined once so every chart in the platform reads as the
     same system. Ordered so the first few colours are the most distinct --
     most charts show three or four series, not eight. */
  window.ctPalette = {
    // Gold leads, as the brand colour. Validated as an ordered set (dataviz
    // validate_palette, light surface): lightness band, chroma floor and
    // normal-vision separation pass; adjacent CVD separation passes for the
    // first six, and slots 7-8 sit in the 6-8 floor band, so charts fold
    // anything past five series into a grey "Other" and ship a table view.
    series: ["#d29000", "#2a78d6", "#eb6834", "#1baf7a", "#4a3aa7", "#e87ba4", "#008300", "#e34948"],
    // Single-series lines: the darker gold clears 3:1 on white.
    brand: "#a87400",
    brandFill: "rgba(255, 204, 0, .18)",
    // Older names for the brand colour, kept so existing charts need no edit.
    green: "#a87400",
    greenFill: "rgba(255, 204, 0, .18)",
    teal: "#14607a",
    tealFill: "rgba(20, 96, 122, .10)",
    danger: "#a32b26",
    muted: "#7f8480",
    // Risk levels run low -> critical and must stay in that order.
    risk: ["#a9cbbf", "#d9c68a", "#cf9187", "#8c3b34"],
    // Outcome states for charts: served / fell back / failed, passed /
    // failed / blocked. Re-stepped from the brand's success, warning and
    // danger tokens -- same hues, spread in lightness -- because the brand
    // trio failed the colour-vision check as chart fills (warning and danger
    // sat at dE 14.4 for full-colour readers). This set passes all-pairs:
    // CVD dE >= 9.2, normal-vision dE >= 21.5. The amber is under 3:1 on
    // white, so every chart using it also ships labels and a table view.
    status: { good: "#2b8a5e", warning: "#cf9530", critical: "#a32b26" },
    // Context in an emphasis chart: present, but not competing.
    context: "#c4ccc9"
  };

  /* Restrained chart defaults: thin gridlines, no heavy borders, small type.
     A chart should read as part of the page, not as a separate application. */
  if (window.Chart) {
    Chart.defaults.font.family = "Inter, -apple-system, 'Segoe UI', Roboto, sans-serif";
    Chart.defaults.font.size = 11;
    Chart.defaults.color = "#6b7472";
    Chart.defaults.borderColor = "#e0e4e3";
    Chart.defaults.plugins.legend.labels.boxWidth = 9;
    Chart.defaults.plugins.legend.labels.boxHeight = 9;
    Chart.defaults.plugins.tooltip.backgroundColor = "#1a1f1d";
    Chart.defaults.plugins.tooltip.padding = 9;
    Chart.defaults.plugins.tooltip.cornerRadius = 2;
    Chart.defaults.elements.bar.borderRadius = 1;
    Chart.defaults.elements.line.borderWidth = 2;
    Chart.defaults.elements.point.radius = 2;
  }

  var csrfToken = null;

  function csrf() {
    if (csrfToken) return csrfToken;
    var match = document.cookie.match(/(^|;\s*)csrftoken=([^;]+)/);
    csrfToken = match ? decodeURIComponent(match[2]) : $("[name=csrfmiddlewaretoken]").val();
    return csrfToken;
  }

  // -- Toasts --------------------------------------------------------------

  function toast(message, tone) {
    if (!message) return;
    var $stack = $(".ct-toast-stack");
    if (!$stack.length) $stack = $('<div class="ct-toast-stack"></div>').appendTo("body");

    var icons = {success: "bi-check-circle", danger: "bi-exclamation-octagon", warning: "bi-exclamation-triangle"};
    var $toast = $('<div class="ct-toast is-' + (tone || "success") + '">' +
      '<i class="bi ' + (icons[tone] || "bi-info-circle") + '"></i>' +
      '<span></span></div>');
    $toast.find("span").text(message);
    $stack.append($toast);

    setTimeout(function () {
      $toast.fadeOut(180, function () { $toast.remove(); });
    }, 4000);
  }

  window.controlTowerToast = toast;

  // -- Shared fetch --------------------------------------------------------

  function swap($target, html) {
    // Replace the target's contents with the matching region from the
    // response, so a view can keep returning a full page.
    var $incoming = $("<div></div>").html(html);
    var selector = "#" + $target.attr("id");
    var $region = $incoming.find(selector);
    $target.html($region.length ? $region.html() : html);
    $target.removeClass("ct-loading");
    $(document).trigger("controltower:swapped", [$target]);
  }

  /* The address bar is deliberately left alone. Pushing the filtered URL would
     give deep-linkable, Back-able views, but it reads as a page reload to
     anyone watching the location bar. Flip this to true to get that behaviour
     back -- nothing else needs to change. */
  var UPDATE_ADDRESS_BAR = false;

  function load($target, url) {
    $target.addClass("ct-loading");
    $.ajax({url: url, headers: {"X-Requested-With": "XMLHttpRequest"}})
      .done(function (html) {
        swap($target, html);
        if (UPDATE_ADDRESS_BAR && window.history && window.history.pushState) {
          window.history.pushState({ajax: true}, "", url);
        }
      })
      .fail(function () {
        $target.removeClass("ct-loading");
        toast("Could not load that view. Showing the previous results.", "warning");
      });
  }

  // -- Filter forms --------------------------------------------------------

  function bindFilters() {
    $(document).on("submit", "form[data-ajax-filter]", function (event) {
      event.preventDefault();
      var $form = $(this);
      var $target = $($form.data("ajax-filter"));
      if (!$target.length) { this.submit(); return; }

      var query = $form.serialize();
      load($target, ($form.attr("action") || window.location.pathname) + "?" + query);
    });

    // Selects and range buttons apply immediately: an extra click to confirm a
    // filter you already chose is friction with no purpose.
    $(document).on("change", "form[data-ajax-filter] select, form[data-ajax-filter] input[type=date]", function () {
      $(this).closest("form").submit();
    });

    $(document).on("click", "a[data-ajax-link]", function (event) {
      var $target = $($(this).data("ajax-link"));
      if (!$target.length) return;
      event.preventDefault();
      load($target, this.href);
    });

    /* Any link inside an AJAX region that points at the *same page* is a
       filter, a reset or a pagination step -- swap the region instead of
       reloading. Links to a different path are real navigation and are left
       alone, as are modified clicks so "open in new tab" still works. */
    $(document).on("click", "[data-ajax-region] a[href]", function (event) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.which === 2) return;
      if (this.target && this.target !== "_self") return;
      if ($(this).is("[data-ajax-link]")) return;

      var href = this.getAttribute("href");
      if (!href || href.charAt(0) === "#") return;

      var url;
      try { url = new URL(this.href, window.location.href); } catch (e) { return; }
      if (url.origin !== window.location.origin) return;
      if (url.pathname !== window.location.pathname) return;

      event.preventDefault();
      load($(this).closest("[data-ajax-region]"), url.pathname + url.search);
    });
  }

  // -- Action forms --------------------------------------------------------

  function bindActions() {
    // Controls that are their own confirmation -- the organization picker being
    // the one that matters -- post as soon as they change. Native form.submit()
    // would skip jQuery's handler and reload the page, so route it through here.
    $(document).on("change", "form[data-ajax-action] [data-ajax-submit]", function () {
      $(this).closest("form").submit();
    });

    $(document).on("submit", "form[data-ajax-action]", function (event) {
      event.preventDefault();
      var $form = $(this);
      var $button = $form.find("[type=submit]").prop("disabled", true);
      var behaviour = $form.data("ajax-action");
      var $refresh = $($form.data("ajax-refresh") || "");
      // Path and query only: a fragment is not part of what the server answers.
      var refreshUrl = window.location.pathname + window.location.search;

      $.ajax({
        url: $form.attr("action"),
        method: "POST",
        data: $form.serialize(),
        headers: {"X-CSRFToken": csrf(), "X-Requested-With": "XMLHttpRequest"}
      })
        .done(function (response) {
          var payload = (typeof response === "object") ? response : {};
          toast(payload.message || $form.data("ajax-message") || "Done.", payload.tone || "success");

          if (behaviour === "remove") {
            $form.closest("[data-ajax-row]").fadeOut(160, function () { $(this).remove(); });
          }
          if ($refresh.length) {
            load($refresh, refreshUrl);
          }
          $(document).trigger("controltower:acted", [payload]);
        })
        .fail(function (xhr) {
          var message = "That action could not be completed.";
          try { message = JSON.parse(xhr.responseText).message || message; } catch (e) {}
          toast(message, "danger");
        })
        .always(function () { $button.prop("disabled", false); });
    });
  }

  // -- Polling regions -----------------------------------------------------

  function bindPolling() {
    function start($region) {
      if ($region.data("ctPolling")) return;
      $region.data("ctPolling", true);

      var url = $region.data("ajax-poll");
      var seconds = parseInt($region.data("ajax-interval"), 10) || 30;
      var timer;

      function refresh(force) {
        // A region that has been swapped away is no longer in the document.
        // Stop its timer rather than let it poll for a page nobody is on.
        if (!$.contains(document.documentElement, $region[0])) {
          clearInterval(timer);
          return;
        }
        // Skip while the tab is hidden: nobody is reading it, and a background
        // tab polling forever is a quiet waste of the user's battery. The first
        // paint is forced regardless -- a region opened in a background tab
        // should hold content, not a spinner, by the time it is looked at.
        if (document.hidden && !force) return;
        $.ajax({url: url, headers: {"X-Requested-With": "XMLHttpRequest"}})
          .done(function (html) { $region.html(html); $(document).trigger("controltower:polled", [$region]); });
      }

      timer = setInterval(refresh, seconds * 1000);
      refresh(true);
      $(document).on("visibilitychange", function () { if (!document.hidden) refresh(); });
    }

    function scan() {
      $("[data-ajax-poll]").each(function () { start($(this)); });
    }

    scan();
    // A swap brings in fresh markup, and with it fresh polling regions.
    $(document).on("controltower:swapped", scan);
  }

  // Unread counters live outside the region an action refreshes, so they are
  // patched from the response rather than left to go stale until the next poll.
  function bindUnreadCounters() {
    $(document).on("controltower:acted", function (event, payload) {
      if (!payload || typeof payload.unread === "undefined") return;
      $("[data-unread-count]").each(function () {
        var $el = $(this);
        if (payload.unread > 0) {
          $el.text(payload.unread + ($el.data("unread-suffix") || "")).show();
        } else {
          $el.hide();
        }
      });
    });
  }

  /* Management forms: `data-show-when="field:value1,value2"` shows a block only
     while another field holds one of those values -- a budget's target follows
     its scope, an MCP server's endpoint follows its transport. A hidden field
     keeps its value; the server decides what counts. */
  function bindConditionalFields() {
    function currentValue(name) {
      var $inputs = $("form [name='" + name + "']");
      if ($inputs.is(":radio")) return $inputs.filter(":checked").val() || "";
      if ($inputs.is(":checkbox")) return $inputs.is(":checked") ? "on" : "";
      return $inputs.val() || "";
    }
    function refresh() {
      $("[data-show-when]").each(function () {
        var rule = String($(this).data("show-when")).split(":");
        var wanted = (rule[1] || "").split(",");
        $(this).toggle(wanted.indexOf(String(currentValue(rule[0]))) !== -1);
      });
    }
    $(document).on("change", "form select, form input", refresh);
    $(document).on("controltower:swapped", refresh);
    refresh();
  }

  /* A control policy's configuration starts from its type's own settings:
     choosing a type fills an empty or untouched configuration with a starter. */
  function bindConfigTemplates() {
    $(document).on("change", "select[data-config-templates]", function () {
      var templates = $(this).data("config-templates") || {};
      var $target = $($(this).data("config-target"));
      var current = $.trim($target.val());
      if (current && current !== $target.data("starter")) return;
      var starter = templates[$(this).val()] || "{}";
      $target.val(starter).data("starter", starter);
    });
  }

  /* The alert rail sticks just below the header, whose height changes with the
     window width (the nav wraps) and the page title. Publish it as a CSS
     variable rather than guessing a number in the stylesheet. */
  function trackHeaderHeight() {
    var header = document.querySelector(".ct-header");
    if (!header) return;
    function publish() {
      document.documentElement.style.setProperty("--ct-header-h", header.offsetHeight + "px");
    }
    publish();
    if (window.ResizeObserver) {
      new ResizeObserver(publish).observe(header);
    } else {
      $(window).on("resize", publish);
    }
  }

  // -- In-page help --------------------------------------------------------

  /* The panel slides over the page when Help is clicked, and closes on a
     click outside it, on Esc, or from its own close button. */
  function bindHelp() {
    var root = document.documentElement;

    function setOpen(open) {
      root.classList.toggle("ct-help-open", open);
      $("[data-help-toggle]").attr("aria-expanded", open ? "true" : "false");
      if (open) $("#ct-help").trigger("focus");
    }

    $(document).on("click", "[data-help-toggle]", function (event) {
      event.stopPropagation();
      setOpen(!root.classList.contains("ct-help-open"));
    });

    $(document).on("click", function (event) {
      if (!root.classList.contains("ct-help-open")) return;
      if ($(event.target).closest("#ct-help").length) return;
      setOpen(false);
    });

    $(document).on("keydown", function (event) {
      if (event.key === "Escape" && root.classList.contains("ct-help-open")) setOpen(false);
    });

    $(document).on("click", "[data-copy]", function () {
      var $button = $(this);
      var text = $button.siblings("pre").text();
      if (!navigator.clipboard) return;
      navigator.clipboard.writeText(text).then(function () {
        $button.find("i").attr("class", "bi bi-clipboard-check");
        setTimeout(function () { $button.find("i").attr("class", "bi bi-clipboard"); }, 1500);
      });
    });

    setOpen(false);
  }

  $(function () {
    trackHeaderHeight();
    bindFilters();
    bindActions();
    bindPolling();
    bindUnreadCounters();
    bindConditionalFields();
    bindConfigTemplates();
    bindHelp();
  });
})(jQuery);
