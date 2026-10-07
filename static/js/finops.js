/* ===========================================================================
   AI FinOps - cost explorer and developer-tools drill-downs
   ---------------------------------------------------------------------------
   Both pages are driven by state held here rather than by page loads: a
   drill-down is a stack of filters plus a dimension, and every click changes
   the state and redraws. Colour follows the entity -- an agent keeps its hue
   while you drill and switch metric -- and beyond five series the rest fold
   into a grey "Other", because the palette is only validated that far.
   =========================================================================== */

(function ($) {
  "use strict";

  var palette = function () { return window.ctPalette || {series: ["#d29000"], context: "#c4ccc9"}; };
  var SERIES_LIMIT = 5;

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  }

  function money(value, digits) {
    var n = Number(value || 0);
    if (digits == null) digits = Math.abs(n) >= 100 ? 0 : 2;
    return "$" + n.toLocaleString(undefined, {minimumFractionDigits: digits, maximumFractionDigits: digits});
  }

  function compact(value) {
    var n = Number(value || 0);
    if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return n.toLocaleString();
  }

  function metricValue(metric, value) {
    return metric === "cost" ? money(value) : compact(value);
  }

  function change(pct) {
    if (pct === null || pct === undefined) return '<span class="text-muted">new</span>';
    var cls = pct > 0 ? "ct-delta-up" : (pct < 0 ? "ct-delta-down" : "text-muted");
    var arrow = pct > 0 ? "bi-arrow-up-right" : (pct < 0 ? "bi-arrow-down-right" : "bi-dash");
    return '<span class="' + cls + '"><i class="bi ' + arrow + '"></i> ' + Math.abs(pct).toFixed(1) + "%</span>";
  }

  function reset(canvas) {
    var existing = window.Chart && Chart.getChart(canvas);
    if (existing) existing.destroy();
    return canvas;
  }

  /* Stable colour per entity: the first five entities seen in a dimension
     take the categorical slots in order and keep them for the session. */
  var assigned = {};
  function colorFor(scope, key) {
    var map = assigned[scope] || (assigned[scope] = {order: []});
    if (key === "__other__") return palette().context;
    var index = map.order.indexOf(key);
    if (index === -1) {
      if (map.order.length >= SERIES_LIMIT) return null;
      map.order.push(key);
      index = map.order.length - 1;
    }
    return palette().series[index];
  }
  function claimColors(scope, keys) {
    // Claim slots for this view's leaders first, so they never go grey
    // because entities from an earlier view used the slots up.
    var map = assigned[scope] || (assigned[scope] = {order: []});
    map.order = map.order.filter(function (k) { return keys.indexOf(k) !== -1; })
      .concat(keys.filter(function (k) { return map.order.indexOf(k) === -1; })).slice(0, SERIES_LIMIT);
  }

  // -- Cost explorer ----------------------------------------------------------

  function Explorer($root) {
    this.$root = $root;
    this.url = $root.data("url");
    this.state = $.extend(true, {}, $root.data("state"));
    this.request = null;
    this.bind();
    this.load();
  }

  Explorer.prototype.query = function (extra) {
    var params = {dim: this.state.dim, metric: this.state.metric, days: this.state.days};
    $.each(this.state.filters, function (key, value) { params["f_" + key] = value; });
    return $.param($.extend(params, extra || {}));
  };

  Explorer.prototype.load = function () {
    var self = this;
    if (this.request) this.request.abort();
    this.$root.addClass("ct-loading");
    this.request = $.getJSON(this.url + "?" + this.query())
      .done(function (data) { self.data = data; self.render(); })
      .fail(function (xhr, status) {
        if (status !== "abort" && window.controlTowerToast) window.controlTowerToast("Could not load cost data.", "warning");
      })
      .always(function () { self.$root.removeClass("ct-loading"); });
    // Keep the address bar shareable without adding history entries.
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, "", window.location.pathname + "?" + this.query());
    }
  };

  Explorer.prototype.drill = function (key) {
    var data = this.data;
    if (!data || !data.next) return;
    this.state.filters[data.dimension] = key;
    this.state.dim = data.next;
    this.load();
  };

  Explorer.prototype.bind = function () {
    var self = this, $root = this.$root;
    $root.on("click", "[data-dim]", function () {
      var dim = $(this).data("dim");
      if (self.state.filters[dim]) return;
      self.state.dim = dim; self.load();
    });
    $root.on("click", "[data-metric]", function () { self.state.metric = $(this).data("metric"); self.load(); });
    $root.on("change", "[data-days]", function () { self.state.days = parseInt(this.value, 10); self.load(); });
    $root.on("click", "[data-crumb]", function () {
      // Back to a level: drop it and everything pinned after it.
      var keep = true, crumb = $(this).data("crumb"), filters = {};
      $.each(self.data.breadcrumbs, function (_, c) {
        if (c.dimension === crumb) keep = false;
        if (keep) filters[c.dimension] = c.key;
      });
      self.state.filters = filters; self.state.dim = crumb; self.load();
    });
    $root.on("click", "[data-crumb-home]", function () {
      self.state.filters = {}; self.state.dim = self.state.path[0]; self.load();
    });
    $root.on("click", "[data-drill]", function (event) {
      event.preventDefault();
      var $el = $(this);
      if ($el.data("drill-dim")) {
        self.state.filters[self.data.dimension] = String($el.data("drill"));
        self.state.dim = $el.data("drill-dim");
        self.load();
      } else {
        self.drill(String($el.data("drill")));
      }
    });
    $root.on("click", "[data-export]", function () {
      window.location.href = self.url + "?" + self.query({format: "csv"});
    });
  };

  Explorer.prototype.render = function () {
    var d = this.data, $r = this.$root, self = this;
    claimColors(d.dimension, d.rows.slice(0, SERIES_LIMIT).map(function (r) { return r.key; }));

    // Controls
    $r.find("[data-metric]").each(function () {
      $(this).toggleClass("active", $(this).data("metric") === d.metric).attr("aria-pressed", $(this).data("metric") === d.metric);
    });
    $r.find("[data-days]").val(String(d.days));
    $r.find("[data-dim]").each(function () {
      var dim = $(this).data("dim");
      $(this).toggleClass("active", dim === d.dimension).prop("disabled", !!d.filters[dim])
        .attr("aria-pressed", dim === d.dimension);
    });

    // Breadcrumbs
    var crumbs = ['<button type="button" class="ct-crumb" data-crumb-home><i class="bi bi-house"></i> All AI spend</button>'];
    $.each(d.breadcrumbs, function (_, c) {
      crumbs.push('<i class="bi bi-chevron-right ct-crumb-sep"></i><button type="button" class="ct-crumb" data-crumb="' +
        esc(c.dimension) + '"><span class="text-muted">' + esc(c.dimension_label) + ':</span> ' + esc(c.label) + "</button>");
    });
    crumbs.push('<i class="bi bi-chevron-right ct-crumb-sep"></i><span class="ct-crumb is-current">by ' +
      esc(d.dimension_label.toLowerCase()) + "</span>");
    $r.find("[data-breadcrumbs]").html(crumbs.join(""));

    // Totals
    var t = d.totals;
    $r.find("[data-total=cost]").text(money(t.cost));
    $r.find("[data-total=change]").html(t.change_pct === null
      ? '<span class="text-muted">No spend in the previous ' + d.days + " days</span>"
      : change(t.change_pct) + ' <span class="text-muted">vs previous ' + d.days + " days</span>");
    $r.find("[data-total=runs]").text(compact(t.runs));
    $r.find("[data-total=tokens]").text(compact(t.tokens));
    $r.find("[data-total=avg]").text(money(t.avg_cost, 4));
    $r.find("[data-total=items]").text(t.items + " " + (t.items === 1 ? d.dimension_label.toLowerCase() : d.dimension_plural.toLowerCase()));
    $r.find("[data-period]").text(d.period.start + " to " + d.period.end);

    // Insights
    $r.find("[data-insights]").html(d.insights.length
      ? d.insights.map(function (note) { return "<li>" + esc(note) + "</li>"; }).join("")
      : '<li class="text-muted">No spend in this slice for the period.</li>');

    $r.find("[data-title=bars]").text(d.metric_label + " by " + d.dimension_label.toLowerCase());
    var shown = Math.min(SERIES_LIMIT, d.rows.length);
    $r.find("[data-title=trend]").text("Daily " + d.metric_label.toLowerCase() + (shown === 1
      ? ", one " + d.dimension_label.toLowerCase()
      : ", top " + shown + " " + d.dimension_plural.toLowerCase()));
    $r.find("[data-next-hint]").html(d.next
      ? 'Click a bar or row to drill into its <strong>' + esc(d.dimensions.filter(function (x) { return x.key === d.next; })[0].label.toLowerCase()) + "</strong> breakdown."
      : "This is the most detailed view for this slice.");

    this.renderBars();
    this.renderTrend();
    this.renderTable();
  };

  Explorer.prototype.renderBars = function () {
    var d = this.data, self = this;
    var canvas = this.$root.find("[data-chart=bars]")[0];
    if (!canvas) return;
    reset(canvas);
    var rows = d.rows.slice(0, 15);
    var $wrap = $(canvas).parent();
    $wrap.css("height", Math.max(160, rows.length * 26 + 40) + "px");
    $wrap.next("[data-empty]").toggle(!rows.length);
    $wrap.toggle(!!rows.length);
    if (!rows.length) return;
    new Chart(canvas, {
      type: "bar",
      data: {
        labels: rows.map(function (r) { return r.label; }),
        datasets: [{
          data: rows.map(function (r) { return r.value; }),
          backgroundColor: rows.map(function (r) { return colorFor(d.dimension, r.key) || palette().context; }),
          borderRadius: 4, borderSkipped: "start", barPercentage: .78, categoryPercentage: .9,
          maxBarThickness: 22
        }]
      },
      options: {
        indexAxis: "y", responsive: true, maintainAspectRatio: false,
        onHover: function (e, els) { e.native.target.style.cursor = (els.length && d.next) ? "pointer" : "default"; },
        onClick: function (e, els) { if (els.length) self.drill(rows[els[0].index].key); },
        plugins: {
          legend: {display: false},
          tooltip: {
            callbacks: {
              label: function (ctx) {
                var r = rows[ctx.dataIndex];
                return [metricValue(d.metric, r.value) + " · " + r.share + "% of total",
                  "Previous period " + money(r.prev_cost) + (r.change_pct == null ? " (new)" : " (" + (r.change_pct > 0 ? "+" : "") + r.change_pct + "%)"),
                  compact(r.runs) + " runs · " + money(r.avg_cost, 4) + " per run"];
              },
              footer: function () { return d.next ? "Click to drill down" : ""; }
            }
          }
        },
        scales: {
          x: {beginAtZero: true, grid: {color: "#eceae4"}, ticks: {callback: function (v) { return metricValue(d.metric, v); }}},
          y: {grid: {display: false}, ticks: {callback: function (v) {
            var label = this.getLabelForValue(v); return label.length > 26 ? label.slice(0, 25) + "…" : label;
          }}}
        }
      }
    });
  };

  Explorer.prototype.renderTrend = function () {
    var d = this.data;
    var canvas = this.$root.find("[data-chart=trend]")[0];
    if (!canvas) return;
    reset(canvas);
    var labels = {};
    $.each(d.rows, function (_, r) { labels[r.key] = r.label; });
    var datasets = d.series.datasets.map(function (s) {
      var color = colorFor(d.dimension, s.key) || palette().context;
      return {
        label: s.key === "__other__" ? "Other" : (labels[s.key] || s.key),
        data: s.data, backgroundColor: color, borderColor: "#ffffff", borderWidth: {top: 1},
        borderRadius: 2, stack: "s"
      };
    });
    var has = datasets.some(function (ds) { return ds.data.some(function (v) { return v > 0; }); });
    $(canvas).parent().toggle(has).next("[data-empty]").toggle(!has);
    if (!has) return;
    new Chart(canvas, {
      type: "bar",
      data: {labels: d.series.labels, datasets: datasets},
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: {mode: "index", intersect: false},
        plugins: {
          legend: {position: "bottom", labels: {boxWidth: 9, font: {size: 10}}},
          tooltip: {
            itemSort: function (a, b) { return b.raw - a.raw; },
            filter: function (item) { return item.raw > 0; },
            callbacks: {label: function (ctx) { return ctx.dataset.label + ": " + metricValue(d.metric, ctx.raw); }}
          }
        },
        scales: {
          x: {stacked: true, grid: {display: false}, ticks: {maxTicksLimit: 10}},
          y: {stacked: true, beginAtZero: true, grid: {color: "#eceae4"},
              ticks: {callback: function (v) { return metricValue(d.metric, v); }}}
        }
      }
    });
  };

  Explorer.prototype.renderTable = function () {
    var d = this.data;
    var others = d.dimensions.filter(function (x) { return !x.pinned && x.key !== d.dimension; });
    var html = d.rows.map(function (r) {
      var color = colorFor(d.dimension, r.key);
      var swatch = '<i class="ct-swatch" style="background:' + (color || palette().context) + '"></i>';
      var drillMenu = "";
      if (d.next) {
        drillMenu = '<div class="btn-group">' +
          '<button type="button" class="btn btn-sm btn-outline-secondary" data-drill="' + esc(r.key) + '" title="Drill down">' +
          '<i class="bi bi-zoom-in"></i></button>' +
          '<button type="button" class="btn btn-sm btn-outline-secondary dropdown-toggle dropdown-toggle-split" data-bs-toggle="dropdown" aria-expanded="false">' +
          '<span class="visually-hidden">Drill by</span></button><ul class="dropdown-menu dropdown-menu-end">' +
          others.map(function (o) {
            return '<li><a class="dropdown-item" href="#" data-drill="' + esc(r.key) + '" data-drill-dim="' + o.key + '"><i class="bi ' +
              o.icon + '"></i> by ' + esc(o.label.toLowerCase()) + "</a></li>";
          }).join("") + "</ul></div>";
      }
      return "<tr>" +
        '<td class="small fw-semibold">' + swatch + esc(r.label) + "</td>" +
        '<td class="small text-end text-nowrap">' + money(r.cost, 2) + "</td>" +
        '<td style="min-width:120px"><div class="ct-share"><span style="width:' + Math.min(r.share, 100) + '%"></span></div>' +
        '<span class="small text-muted">' + r.share.toFixed(1) + "%</span></td>" +
        '<td class="small text-end text-nowrap">' + change(r.change_pct) + "</td>" +
        '<td class="small text-end">' + r.runs.toLocaleString() + "</td>" +
        '<td class="small text-end">' + compact(r.tokens) + "</td>" +
        '<td class="small text-end text-nowrap">' + money(r.avg_cost, 4) + "</td>" +
        '<td class="small text-end text-nowrap">' + (r.failed_cost ? money(r.failed_cost, 2) : '<span class="text-muted">—</span>') + "</td>" +
        '<td class="text-end">' + drillMenu + "</td></tr>";
    }).join("");
    this.$root.find("[data-table] tbody").html(html ||
      '<tr><td colspan="9"><div class="ct-empty"><i class="bi bi-cash-stack"></i><p class="mb-0">No spend in this slice for the period.</p></div></td></tr>');
    this.$root.find("[data-table-label]").text(d.dimension_label);
    this.$root.find("[data-table-share]").text("Share of " + d.metric_label.toLowerCase());
  };

  // -- Developer tools ---------------------------------------------------------

  function DevTools($root) {
    this.$root = $root;
    this.data = JSON.parse($root.find("script[data-devtools]").text());
    this.team = this.data.initial_team || "";
    this.sort = {key: "total", desc: true};
    this.search = "";
    this.bind();
    this.render();
  }

  DevTools.prototype.bind = function () {
    var self = this, $r = this.$root;
    $r.on("change", "[data-team-filter]", function () { self.team = this.value; self.render(); });
    $r.on("click", "[data-team-clear]", function () { self.team = ""; self.render(); });
    $r.on("input", "[data-search]", function () { self.search = this.value.toLowerCase(); self.renderTable(); });
    $r.on("click", "[data-sort]", function () {
      var key = $(this).data("sort");
      self.sort = {key: key, desc: self.sort.key === key ? !self.sort.desc : true};
      self.renderTable();
    });
    $r.on("click", "[data-person]", function (event) {
      if ($(event.target).closest("a, button").length) return;
      $(this).next(".ct-person-detail").toggleClass("d-none");
      $(this).attr("aria-expanded", function (_, v) { return v === "true" ? "false" : "true"; });
    });
  };

  DevTools.prototype.people = function () {
    var team = this.team;
    return this.data.individuals.filter(function (p) { return !team || p.team === team; });
  };

  DevTools.prototype.render = function () {
    var self = this, $r = this.$root;
    var options = ['<option value="">All teams</option>'].concat(this.data.teams.map(function (t) {
      return '<option value="' + esc(t.team) + '"' + (t.team === self.team ? " selected" : "") + ">" + esc(t.team) + "</option>";
    }));
    $r.find("[data-team-filter]").html(options.join(""));
    $r.find("[data-team-clear]").toggle(!!this.team);
    $r.find("[data-drill-title]").text(this.team ? "Individuals in " + this.team : "Individuals, all teams");
    this.renderTeams();
    this.renderTrend();
    this.renderPeopleChart();
    this.renderTable();
  };

  function stackedBar(canvas, rows, opts) {
    reset(canvas);
    var p = palette();
    return new Chart(canvas, {
      type: "bar",
      data: {
        labels: rows.map(function (r) { return r.label; }),
        datasets: [
          {label: "Seat licences", data: rows.map(function (r) { return r.license; }), backgroundColor: p.series[0],
           borderColor: "#ffffff", borderWidth: {right: 2}, borderSkipped: false, stack: "s", maxBarThickness: 22},
          {label: "Premium request overage", data: rows.map(function (r) { return r.premium; }), backgroundColor: p.series[1],
           borderRadius: 4, borderSkipped: "start", stack: "s", maxBarThickness: 22}
        ]
      },
      options: {
        indexAxis: "y", responsive: true, maintainAspectRatio: false,
        interaction: {mode: "index", intersect: false},
        onHover: function (e, els) { e.native.target.style.cursor = (els.length && opts.onClick) ? "pointer" : "default"; },
        onClick: function (e, els) { if (els.length && opts.onClick) opts.onClick(rows[els[0].index]); },
        plugins: {
          legend: {position: "bottom", labels: {boxWidth: 9, font: {size: 10}}},
          tooltip: {callbacks: {
            label: function (ctx) { return ctx.dataset.label + ": " + money(ctx.raw, 2); },
            footer: function (items) {
              var r = rows[items[0].dataIndex];
              return ["Total " + money(r.license + r.premium, 2)].concat(r.extra || [], opts.onClick ? ["Click for the individuals"] : []);
            }
          }}
        },
        scales: {
          x: {stacked: true, beginAtZero: true, grid: {color: "#eceae4"}, ticks: {callback: function (v) { return money(v, 0); }}},
          y: {stacked: true, grid: {display: false}}
        }
      }
    });
  }

  DevTools.prototype.renderTeams = function () {
    var self = this, canvas = this.$root.find("[data-chart=teams]")[0];
    if (!canvas) return;
    var rows = this.data.teams.map(function (t) {
      return {label: t.team, license: t.license, premium: t.premium_cost,
        extra: [t.seats + " seats · " + money(t.per_seat, 2) + " per seat", t.idle_seats + " idle"]};
    });
    $(canvas).parent().css("height", Math.max(140, rows.length * 34 + 60) + "px");
    stackedBar(canvas, rows, {onClick: function (row) { self.team = row.label; self.render(); }});
  };

  DevTools.prototype.renderTrend = function () {
    var canvas = this.$root.find("[data-chart=trend]")[0];
    if (!canvas) return;
    reset(canvas);
    var trend = this.data.trend, team = this.team;
    var series = trend.series.slice().sort(function (a, b) {
      return b.data.reduce(function (s, v) { return s + v; }, 0) - a.data.reduce(function (s, v) { return s + v; }, 0);
    });
    if (team) series = series.filter(function (s) { return s.label === team; });
    claimColors("devtools-team", series.slice(0, SERIES_LIMIT).map(function (s) { return s.label; }));
    var top = series.slice(0, SERIES_LIMIT), rest = series.slice(SERIES_LIMIT);
    var datasets = top.map(function (s) {
      return {label: s.label, data: s.data, backgroundColor: colorFor("devtools-team", s.label), borderColor: "#ffffff",
        borderWidth: {top: 1}, borderRadius: 2, stack: "s"};
    });
    if (rest.length) {
      datasets.push({label: "Other", stack: "s", backgroundColor: palette().context, borderRadius: 2,
        data: trend.labels.map(function (_, i) { return rest.reduce(function (s, x) { return s + x.data[i]; }, 0); })});
    }
    new Chart(canvas, {
      type: "bar",
      data: {labels: trend.labels, datasets: datasets},
      options: {
        responsive: true, maintainAspectRatio: false, interaction: {mode: "index", intersect: false},
        plugins: {
          legend: {position: "bottom", labels: {boxWidth: 9, font: {size: 10}}},
          tooltip: {itemSort: function (a, b) { return b.raw - a.raw; },
            callbacks: {label: function (ctx) { return ctx.dataset.label + ": " + money(ctx.raw, 2); }}}
        },
        scales: {
          x: {stacked: true, grid: {display: false}},
          y: {stacked: true, beginAtZero: true, grid: {color: "#eceae4"}, ticks: {callback: function (v) { return money(v, 0); }}}
        }
      }
    });
  };

  DevTools.prototype.renderPeopleChart = function () {
    var canvas = this.$root.find("[data-chart=people]")[0];
    if (!canvas) return;
    var people = this.people().slice(0, 15);
    var rows = people.map(function (p) {
      return {label: p.name, license: p.license, premium: p.premium_cost,
        extra: [p.premium_requests.toLocaleString() + " premium requests (" + p.allowance_use + "% of allowance at peak)",
          p.idle_days === null ? "Never used" : (p.idle_days === 0 ? "Active today" : "Last active " + p.idle_days + " days ago")]};
    });
    $(canvas).parent().css("height", Math.max(140, rows.length * 26 + 60) + "px");
    stackedBar(canvas, rows, {});
  };

  DevTools.prototype.renderTable = function () {
    var self = this, sort = this.sort, search = this.search;
    var people = this.people().filter(function (p) {
      return !search || (p.name + " " + p.login + " " + p.team + " " + p.email).toLowerCase().indexOf(search) !== -1;
    });
    people.sort(function (a, b) {
      var x = a[sort.key], y = b[sort.key];
      if (x === null) x = sort.key === "idle_days" ? 1e9 : -1;
      if (y === null) y = sort.key === "idle_days" ? 1e9 : -1;
      var r = (typeof x === "string") ? x.localeCompare(y) : x - y;
      return sort.desc ? -r : r;
    });
    this.$root.find("[data-sort]").each(function () {
      var key = $(this).data("sort");
      $(this).find("i").attr("class", "bi " + (key === sort.key ? (sort.desc ? "bi-sort-down" : "bi-sort-up") : "bi-arrow-down-up opacity-25"));
    });
    var editUrl = this.data.seat_edit_url, canEdit = this.$root.data("can-manage") === true || this.$root.data("can-manage") === "true";
    var html = people.map(function (p) {
      var idle;
      if (!p.is_active) idle = '<span class="badge badge-soft text-bg-secondary">Removed</span>';
      else if (p.idle_days === null) idle = '<span class="badge badge-soft text-bg-danger">Never used</span>';
      else if (p.idle_days >= 30) idle = '<span class="badge badge-soft text-bg-warning">Idle ' + p.idle_days + "d</span>";
      else idle = '<span class="small text-muted">' + (p.idle_days === 0 ? "today" : p.idle_days + "d ago") + "</span>";
      var use = Math.min(p.allowance_use, 100);
      var models = Object.keys(p.by_model || {}).sort(function (a, b) { return p.by_model[b] - p.by_model[a]; })
        .map(function (m) { return '<span class="badge text-bg-light me-1">' + esc(m) + " · " + Math.round(p.by_model[m]).toLocaleString() + "</span>"; }).join("");
      return '<tr data-person tabindex="0" aria-expanded="false" class="ct-clickable">' +
        '<td class="small"><span class="fw-semibold">' + esc(p.name) + '</span><div class="text-muted"><i class="bi bi-github"></i> ' + esc(p.login) + "</div></td>" +
        '<td class="small">' + esc(p.team) + '<div class="text-muted">' + esc(p.department) + "</div></td>" +
        '<td class="small">' + esc(p.plan) + "</td>" +
        "<td>" + idle + "</td>" +
        '<td class="small text-end">' + Math.round(p.premium_requests).toLocaleString() + "</td>" +
        '<td style="min-width:110px"><div class="ct-share' + (p.allowance_use > 100 ? " is-over" : "") + '"><span style="width:' + use + '%"></span></div>' +
        '<span class="small text-muted">' + p.allowance_use + "% of " + p.allowance + "</span></td>" +
        '<td class="small text-end text-nowrap">' + money(p.license, 2) + "</td>" +
        '<td class="small text-end text-nowrap">' + (p.premium_cost ? money(p.premium_cost, 2) : '<span class="text-muted">—</span>') + "</td>" +
        '<td class="small text-end text-nowrap fw-semibold">' + money(p.total, 2) + "</td>" +
        '<td class="text-end">' + (canEdit ? '<a class="btn btn-sm btn-outline-secondary" title="Attribute to a person and team" href="' +
          editUrl.replace("00000000-0000-0000-0000-000000000000", p.seat_id) + '"><i class="bi bi-person-gear"></i></a>' : "") + "</td></tr>" +
        '<tr class="ct-person-detail d-none"><td colspan="10" class="small">' +
        '<div class="d-flex flex-wrap gap-4"><div><span class="text-muted">Premium requests by model</span><div class="mt-1">' + (models || '<span class="text-muted">None this period</span>') + "</div></div>" +
        '<div><span class="text-muted">Overage requests</span><div class="fw-semibold">' + Math.round(p.overage_requests).toLocaleString() + "</div></div>" +
        '<div><span class="text-muted">Last editor</span><div>' + esc(p.editor || "—") + "</div></div>" +
        '<div><span class="text-muted">Email</span><div>' + esc(p.email || "—") + "</div></div></div></td></tr>";
    }).join("");
    this.$root.find("[data-people] tbody").html(html ||
      '<tr><td colspan="10"><div class="ct-empty"><i class="bi bi-people"></i><p class="mb-0">No seats match.</p></div></td></tr>');
    this.$root.find("[data-people-count]").text(people.length + " seat" + (people.length === 1 ? "" : "s"));
  };

  // -- Small charts used by server-rendered pages ------------------------------

  function burnChart(canvas, data) {
    reset(canvas);
    var p = palette();
    var flat = function (v) { return data.labels.map(function () { return v; }); };
    var datasets = [
      {label: "Actual", data: data.actual, borderColor: p.brand, backgroundColor: p.brandFill, fill: true, tension: .2, pointRadius: 0},
      {label: "Forecast", data: data.forecast, borderColor: p.brand, borderDash: [5, 4], pointRadius: 0, fill: false, spanGaps: true},
      {label: "Budget", data: flat(data.budget), borderColor: "#1a1a1a", borderWidth: 1.5, pointRadius: 0, fill: false}
    ];
    $.each(data.thresholds, function (_, t) {
      if (t.pct >= 100) return;
      datasets.push({label: t.pct + "% threshold", data: flat(t.value), borderColor: "#a9aba6", borderWidth: 1,
        borderDash: [2, 3], pointRadius: 0, fill: false});
    });
    return new Chart(canvas, {
      type: "line",
      data: {labels: data.labels, datasets: datasets},
      options: {
        responsive: true, maintainAspectRatio: false, interaction: {mode: "index", intersect: false},
        plugins: {
          legend: {position: "bottom", labels: {boxWidth: 9, font: {size: 10}, filter: function (i) { return i.text.indexOf("threshold") === -1; }}},
          tooltip: {filter: function (i) { return i.raw !== null && i.dataset.label.indexOf("threshold") === -1; },
            callbacks: {label: function (ctx) { return ctx.dataset.label + ": " + money(ctx.raw, 2); }}}
        },
        scales: {x: {grid: {display: false}, ticks: {maxTicksLimit: 10}},
                 y: {beginAtZero: true, grid: {color: "#eceae4"}, ticks: {callback: function (v) { return money(v, 0); }}}}
      }
    });
  }

  function savingsChart(canvas, data) {
    reset(canvas);
    return new Chart(canvas, {
      type: "bar",
      data: {labels: data.labels, datasets: [{data: data.values, backgroundColor: palette().series[0],
        borderRadius: 4, borderSkipped: "start", barPercentage: .7, maxBarThickness: 22}]},
      options: {
        indexAxis: "y", responsive: true, maintainAspectRatio: false,
        plugins: {legend: {display: false}, tooltip: {callbacks: {label: function (ctx) {
          return money(ctx.raw, 2) + " a month · " + data.counts[ctx.dataIndex] + " recommendation" + (data.counts[ctx.dataIndex] === 1 ? "" : "s");
        }}}},
        scales: {x: {beginAtZero: true, grid: {color: "#eceae4"}, ticks: {callback: function (v) { return money(v, 0); }}},
                 y: {grid: {display: false}}}
      }
    });
  }

  // A clicked submit button's value is not in $.serialize(); carry it in the
  // form's hidden field so an AJAX post says which decision was made.
  $(document).on("click", "form [name=decision][type=submit]", function () {
    $(this).closest("form").find("[data-decision]").val(this.value);
  });

  // -- Boot -----------------------------------------------------------------------

  function boot() {
    $("[data-finops-explorer]").each(function () {
      if (!$(this).data("finopsBooted")) { $(this).data("finopsBooted", true); new Explorer($(this)); }
    });
    $("[data-finops-devtools]").each(function () {
      if (!$(this).data("finopsBooted")) { $(this).data("finopsBooted", true); new DevTools($(this)); }
    });
    $("canvas[data-burn]").each(function () { burnChart(this, JSON.parse($("#" + $(this).data("burn")).text())); });
    $("canvas[data-savings]").each(function () { savingsChart(this, JSON.parse($("#" + $(this).data("savings")).text())); });
  }

  window.FinOps = {money: money, compact: compact, burnChart: burnChart};
  $(boot);
  // Same-page filter links swap the content region; rebuild what came in.
  $(document).on("controltower:swapped", boot);
})(jQuery);
