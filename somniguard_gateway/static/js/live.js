/* SOMNI-Guard live monitor.
 * Polls /live/data every few seconds and re-renders vitals tiles +
 * sparklines as inline SVG.  No third-party libraries — runs under a
 * strict CSP (script-src 'self').
 */
(function () {
  "use strict";

  var POLL_MS = 4000;
  var grid = document.getElementById("live-grid");
  var lastUpdate = document.getElementById("last-update");
  if (!grid) return;

  var SVG_NS = "http://www.w3.org/2000/svg";
  var SVG_TAGS = { svg: 1, polyline: 1, rect: 1, line: 1, circle: 1, g: 1, path: 1 };

  function el(tag, attrs, children) {
    var node = SVG_TAGS[tag]
      ? document.createElementNS(SVG_NS, tag)
      : document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) {
      if (c == null) return;
      node.appendChild(typeof c === "string"
        ? document.createTextNode(c) : c);
    });
    return node;
  }

  function sparkline(series, color) {
    if (!series || series.length < 2) {
      return el("div", { "class": "muted" }, ["—"]);
    }
    var w = 240, h = 36, pad = 2;
    var lo = Math.min.apply(null, series);
    var hi = Math.max.apply(null, series);
    if (hi === lo) { hi = lo + 1; }
    var step = (w - pad * 2) / (series.length - 1);
    var points = series.map(function (v, i) {
      var x = pad + i * step;
      var y = h - pad - ((v - lo) / (hi - lo)) * (h - pad * 2);
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    var svg = el("svg", {
      "class": "spark",
      viewBox: "0 0 " + w + " " + h,
      preserveAspectRatio: "none",
    });
    svg.appendChild(el("polyline", {
      points: points,
      fill: "none",
      stroke: color,
      "stroke-width": "2",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
    }));
    return svg;
  }

  function fmt(v, suffix, digits) {
    if (v == null || isNaN(v)) return "—";
    return Number(v).toFixed(digits == null ? 0 : digits) + (suffix || "");
  }

  function severityClass(sessions) {
    var crit = 0, warn = 0;
    sessions.unack_alerts.forEach(function (a) {
      if (a.severity === "critical") crit += 1;
      else warn += 1;
    });
    if (crit > 0) return "crit";
    if (warn > 0) return "warn";
    return "";
  }

  function tile(s) {
    var sev = severityClass(s);
    var head = el("h3", {}, [
      s.patient_name + (s.patient_mrn ? " (MRN " + s.patient_mrn + ")" : ""),
    ]);
    var meta = el("div", { "class": "muted" }, [
      "Session #" + s.session_id + " · device " + s.device_id +
      " · started " + (s.started_at || "—"),
    ]);

    var spo2Val = s.latest && s.latest.spo2 != null
      ? fmt(s.latest.spo2, "%", 1) : "—";
    var hrVal = s.latest && s.latest.hr != null
      ? fmt(s.latest.hr, " bpm", 0) : "—";

    var spo2Vital = el("div", { "class": "vital" }, [
      el("div", { "class": "lbl" }, ["SpO₂"]),
      el("div", { "class": "val" }, [spo2Val]),
      sparkline(s.spo2_recent, "#0891b2"),
    ]);
    var hrVital = el("div", { "class": "vital" }, [
      el("div", { "class": "lbl" }, ["Heart rate"]),
      el("div", { "class": "val" }, [hrVal]),
      sparkline(s.hr_recent, "#dc2626"),
    ]);
    var row = el("div", { "class": "vitals-row" }, [spo2Vital, hrVital]);

    var alertList = null;
    if (s.unack_alerts && s.unack_alerts.length) {
      alertList = el("div", { "style": "margin-top:.6rem;" }, [
        el("div", { "class": "muted" }, ["Open alerts:"]),
        el("ul", { "style": "margin:.25rem 0 0 1.1rem; padding:0;" },
          s.unack_alerts.map(function (a) {
            return el("li", {}, [
              el("span", {
                "class": "badge badge-" +
                  (a.severity === "critical" ? "critical" : "warning"),
              }, [a.severity]),
              " " + (a.message || a.alert_type) +
              " (" + (a.triggered_at || "") + ")",
            ]);
          })),
      ]);
    }

    var actions = el("div", { "style": "margin-top:.6rem;" }, [
      (function () {
        var a = el("a", {
          "class": "btn btn-outline btn-sm",
          href: "/sessions/" + s.session_id,
        }, ["Open session"]);
        return a;
      })(),
    ]);

    return el("div", { "class": "vitals-tile " + sev }, [
      head, meta, row, alertList, actions,
    ]);
  }

  function render(payload) {
    grid.innerHTML = "";
    if (!payload.sessions || payload.sessions.length === 0) {
      grid.appendChild(el("p", { "class": "muted" }, [
        "No active sessions. Power on a Pico device to begin monitoring.",
      ]));
    } else {
      payload.sessions.forEach(function (s) {
        grid.appendChild(tile(s));
      });
    }
    if (lastUpdate) {
      var now = new Date();
      lastUpdate.textContent = "Updated " + now.toLocaleTimeString();
    }
  }

  function poll() {
    fetch("/live/data", { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(render)
      .catch(function (e) {
        if (lastUpdate) {
          lastUpdate.textContent = "Update failed: " + e.message;
        }
      })
      .then(function () {
        setTimeout(poll, POLL_MS);
      });
  }

  poll();
}());
