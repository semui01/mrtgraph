/* MRT All-Stations Planner – front end (no dependencies). */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const SVGNS = "http://www.w3.org/2000/svg";
  const QUICK = ["Bedok", "Orchard", "Jurong East", "Changi Airport", "HarbourFront", "Woodlands North", "Dhoby Ghaut"];

  const state = {
    net: null,
    stationsByName: new Map(),
    lineColor: new Map(),
    pos: new Map(),          // station -> [x, y]
    segOffset: new Map(),    // "line|a|b" -> [dx, dy]
    view: null, home: null,  // current / initial viewBox {x, y, w, h}
    plan: null,
    step: 0,
    timer: null,
    firstVisit: new Map(),   // station -> {step, clock}
    visits: [],              // [{t, n, station, step}] first-visit times in order
    upcoming: false,
    polling: null,
  };

  // ------------------------------------------------------------------ helpers
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtClock = (m) => { const t = Math.round(m); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`; };
  const fmtHM = (m) => { const t = Math.round(m); const h = Math.floor(t / 60), mm = t % 60; return h ? `${h} h ${mm} min` : `${mm} min`; };
  const fmtHMc = (m) => { const t = Math.round(m); const h = Math.floor(t / 60), mm = t % 60; return h ? `${h}h ${String(mm).padStart(2, "0")}m` : `${mm}m`; };
  const fmtMin = (m) => `${+m.toFixed(1)} min`;
  const el = (tag, attrs = {}, parent = null) => {
    const node = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (parent) parent.appendChild(node);
    return node;
  };
  const lineName = (id) => (state.net?.lines.find((l) => l.id === id) || {}).name || id;
  const pill = (line) => `<span class="pill" style="background:${state.lineColor.get(line) || "#666"}">${esc(line)}</span>`;
  const codeChips = (st) => st.codes.map(([ln, code]) => `<span class="code" style="background:${state.lineColor.get(ln)}">${esc(code)}</span>`).join(" ");

  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(body.error || `Request failed (${res.status})`);
      err.body = body;
      throw err;
    }
    return body;
  }

  // ------------------------------------------------------------------ network + map
  async function loadNetwork(upcoming) {
    state.upcoming = upcoming;
    const net = await api(`/api/network?upcoming=${upcoming ? 1 : 0}`);
    state.net = net;
    state.stationsByName = new Map(net.stations.map((s) => [s.name, s]));
    state.lineColor = new Map(net.lines.map((l) => [l.id, l.color]));
    $("#reversal").placeholder = net.defaults.reversal_penalty_min;
    if ($("#reversal").value === "") $("#reversal").value = net.defaults.reversal_penalty_min;
    if ($("#time-limit").value === "") $("#time-limit").value = net.defaults.time_limit_s;
    const s = net.summary;
    $("#network-meta").textContent = `${s.stations} stations · ${s.interchanges} interchanges · ${s.platform_nodes} platform nodes · network as of ${net.as_of}`;
    $("#upcoming-date").textContent = new Date(net.upcoming_date + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
    renderFootnote();
    renderLegend();
    buildGeometry();
    drawMap();
  }

  function renderFootnote() {
    const n = state.net;
    const notes = [
      `Network as of ${n.as_of}: NSL, EWL (incl. Changi Airport branch), NEL, CCL (full loop since Stage 6, 12 Jul 2026), DTL and TEL. LRT lines are excluded and transfers happen only inside official interchange stations.`,
      `Ride times are estimates of about 2–3 min per stop (from station spacing). Interchange penalties of 5–10 min cover walking and waiting.`,
    ];
    if (n.not_yet_open.length) {
      notes.push(`Not yet open on this date: ${[...new Set(n.not_yet_open.map((s) => s.station))].join(", ")} (from ${n.upcoming_date}).`);
    }
    $("#footnote").innerHTML = notes.map((t) => `<p>${esc(t)}</p>`).join("");
  }

  function renderLegend() {
    const shown = new Set();
    $("#legend").innerHTML = state.net.lines
      .filter((l) => { const k = l.parent || l.id; if (shown.has(k)) return false; shown.add(k); return true; })
      .map((l) => `<span><i style="background:${l.color}"></i>${esc(l.name.replace(/ \(.*\)/, ""))}</span>`)
      .join("");
  }

  function buildGeometry() {
    const st = state.net.stations;
    const lat0 = st.reduce((a, s) => a + s.lat, 0) / st.length;
    const kx = Math.cos((lat0 * Math.PI) / 180);
    const xs = st.map((s) => s.lon * kx), ys = st.map((s) => -s.lat);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const scale = 1000 / (maxX - minX);
    state.pos.clear();
    st.forEach((s, i) => state.pos.set(s.name, [(xs[i] - minX) * scale, (ys[i] - minY) * scale]));
    const h = (maxY - minY) * scale;
    const pad = 30;
    state.home = { x: -pad, y: -pad, w: 1000 + 2 * pad, h: h + 2 * pad };
    state.view = { ...state.home };
    $(".map-wrap").style.aspectRatio = `${state.home.w} / ${state.home.h}`;

    // parallel offsets for line pairs sharing a station pair (e.g. NSL+EWL City Hall–Raffles Place)
    const byPair = new Map();
    for (const g of state.net.segments) {
      const key = [g.a, g.b].sort().join("|");
      if (!byPair.has(key)) byPair.set(key, []);
      byPair.get(key).push(g.line);
    }
    state.segOffset.clear();
    for (const [key, lines] of byPair) {
      const [a, b] = key.split("|");
      const [x1, y1] = state.pos.get(a), [x2, y2] = state.pos.get(b);
      const len = Math.hypot(x2 - x1, y2 - y1) || 1;
      const nx = -(y2 - y1) / len, ny = (x2 - x1) / len;
      lines.forEach((ln, i) => {
        const d = (i - (lines.length - 1) / 2) * 3.4;
        state.segOffset.set(`${ln}|${a}|${b}`, [nx * d, ny * d]);
        state.segOffset.set(`${ln}|${b}|${a}`, [nx * d, ny * d]);
      });
    }
  }

  const P = (name) => state.pos.get(name);
  function segPoints(line, a, b) {
    const [dx, dy] = state.segOffset.get(`${line}|${a}|${b}`) || [0, 0];
    const [x1, y1] = P(a), [x2, y2] = P(b);
    return [[x1 + dx, y1 + dy], [x2 + dx, y2 + dy]];
  }

  function drawMap() {
    const svg = $("#map");
    svg.innerHTML = "";
    const base = el("g", { id: "base" }, svg);
    for (const g of state.net.segments) {
      const [[x1, y1], [x2, y2]] = segPoints(g.line, g.a, g.b);
      el("line", {
        x1, y1, x2, y2, class: "seg base", stroke: state.lineColor.get(g.line),
        "stroke-width": 3.2, "vector-effect": "non-scaling-stroke",
      }, base);
    }
    el("g", { id: "route" }, svg);
    const stations = el("g", { id: "stations" }, svg);
    for (const s of state.net.stations) {
      const [x, y] = P(s.name);
      const c = el("circle", { cx: x, cy: y, r: 3, class: "st", "stroke-width": s.interchange ? 1.8 : 1.4, "vector-effect": "non-scaling-stroke" }, stations);
      c.dataset.name = s.name;
      c.addEventListener("pointerenter", (ev) => showTip(ev, s));
      c.addEventListener("pointermove", (ev) => moveTip(ev));
      c.addEventListener("pointerleave", hideTip);
      c.addEventListener("click", () => { $("#start").value = s.name; });
    }
    const labels = el("g", { id: "labels" }, svg);
    const terminals = terminalSet();
    for (const s of state.net.stations) {
      const [x, y] = P(s.name);
      const t = el("text", { x: x + 5, y: y - 5, class: "st-label" }, labels);
      t.textContent = s.name;
      t.dataset.name = s.name;
      t.dataset.major = s.interchange || terminals.has(s.name) ? "1" : "0";
    }
    el("g", { id: "markers" }, svg);
    applyView();
    if (state.plan) renderRoute();
  }

  function terminalSet() {
    const deg = new Map();
    for (const g of state.net.segments) {
      deg.set(`${g.line}|${g.a}`, (deg.get(`${g.line}|${g.a}`) || 0) + 1);
      deg.set(`${g.line}|${g.b}`, (deg.get(`${g.line}|${g.b}`) || 0) + 1);
    }
    const out = new Set();
    for (const [k, d] of deg) if (d === 1) out.add(k.split("|")[1]);
    return out;
  }

  function unitsPerPixel() {
    const svg = $("#map");
    const w = svg.clientWidth || 800, h = svg.clientHeight || 500;
    return Math.max(state.view.w / w, state.view.h / h);
  }

  function applyView() {
    const v = state.view;
    if (!v) return;
    const svg = $("#map");
    svg.setAttribute("viewBox", `${v.x} ${v.y} ${v.w} ${v.h}`);
    const upp = unitsPerPixel();
    const zoomed = v.w < state.home.w * 0.55;
    // lighter marks when the map is small on screen (phones)
    const k = Math.max(0.6, Math.min(1, (svg.clientWidth || 900) / 900 * (state.home.w / v.w)));
    svg.querySelectorAll("circle.st").forEach((c) => {
      const s = state.stationsByName.get(c.dataset.name);
      c.setAttribute("r", (s.interchange ? 4.6 : 3.1) * upp * k);
    });
    svg.querySelectorAll("line.seg.base").forEach((l) => l.setAttribute("stroke-width", 3.2 * Math.max(0.7, k)));
    placeLabels(svg, upp, zoomed);
    svg.querySelectorAll("[data-scale]").forEach((m) => m.setAttribute("r", +m.dataset.scale * upp));
  }

  // Greedy label placement: important labels first, try right then left, drop on collision.
  function placeLabels(svg, upp, zoomed) {
    const small = (svg.clientWidth || 900) < 600;
    const fs = (small ? 9 : zoomed ? 11 : 10) * upp;
    const pad = 1.5 * upp;
    const emphasis = new Set();
    if (state.plan) {
      emphasis.add(state.plan.start_station);
      emphasis.add(state.plan.summary.end_station);
      const cur = state.plan.steps[state.step];
      if (cur) emphasis.add(cur.to_station || cur.station);
    }
    const labels = [...svg.querySelectorAll("text.st-label")];
    const rank = (t) => {
      const name = t.dataset.name;
      if (emphasis.has(name)) return 0;
      const s = state.stationsByName.get(name);
      if (t.dataset.major === "1") return s.interchange ? 3 - Math.min(2, s.codes.length - 1) : 1;
      return 4;
    };
    labels.sort((a, b) => rank(a) - rank(b));
    const placed = [];
    const hit = (b) => placed.some((p) => b.x0 < p.x1 && b.x1 > p.x0 && b.y0 < p.y1 && b.y1 > p.y0);
    const v = state.view;
    for (const t of labels) {
      const name = t.dataset.name;
      const s = state.stationsByName.get(name);
      const wanted = zoomed || emphasis.has(name) || (t.dataset.major === "1" && !(small && s.interchange));
      t.style.display = "none";
      if (!wanted) continue;
      const [x, y] = P(name);
      const w = name.length * fs * 0.56, off = 6 * upp;
      const right = { x0: x + off - pad, x1: x + off + w + pad, y0: y - off - fs - pad, y1: y - off + pad, tx: x + off };
      const left = { x0: x - off - w - pad, x1: x - off + pad, y0: right.y0, y1: right.y1, tx: x - off - w };
      const fitsRight = right.x1 <= v.x + v.w;
      const choice = [fitsRight ? right : left, fitsRight ? left : right].find((b) => !hit(b));
      if (!choice) continue;
      placed.push(choice);
      t.style.fontSize = `${fs}px`;
      t.setAttribute("x", choice.tx);
      t.setAttribute("y", y - off);
      t.style.strokeWidth = `${3 * upp}px`;
      t.style.fontWeight = emphasis.has(name) ? "700" : "";
      t.style.display = "";
    }
  }

  function zoomAt(factor, cx, cy) {
    const v = state.view;
    const minW = state.home.w / 12, maxW = state.home.w * 1.6;
    const w = Math.min(maxW, Math.max(minW, v.w * factor));
    const k = w / v.w;
    state.view = { x: cx - (cx - v.x) * k, y: cy - (cy - v.y) * k, w, h: v.h * k };
    applyView();
  }

  function clientToMap(ev) {
    const svg = $("#map");
    const pt = svg.createSVGPoint();
    pt.x = ev.clientX; pt.y = ev.clientY;
    const p = pt.matrixTransform(svg.getScreenCTM().inverse());
    return [p.x, p.y];
  }

  function setupMapInteraction() {
    const svg = $("#map");
    svg.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      const [cx, cy] = clientToMap(ev);
      zoomAt(ev.deltaY > 0 ? 1.15 : 1 / 1.15, cx, cy);
    }, { passive: false });
    const pointers = new Map();
    let last = null, pinch = null;
    svg.addEventListener("pointerdown", (ev) => {
      if (ev.target.classList.contains("st")) return;
      svg.setPointerCapture(ev.pointerId);
      pointers.set(ev.pointerId, [ev.clientX, ev.clientY]);
      svg.classList.add("dragging");
      last = [ev.clientX, ev.clientY];
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinch = Math.hypot(a[0] - b[0], a[1] - b[1]);
      }
    });
    svg.addEventListener("pointermove", (ev) => {
      if (!pointers.has(ev.pointerId)) return;
      pointers.set(ev.pointerId, [ev.clientX, ev.clientY]);
      if (pointers.size === 2 && pinch) {
        const [a, b] = [...pointers.values()];
        const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
        const mid = { clientX: (a[0] + b[0]) / 2, clientY: (a[1] + b[1]) / 2 };
        const [cx, cy] = clientToMap(mid);
        zoomAt(pinch / d, cx, cy);
        pinch = d;
        return;
      }
      const upp = unitsPerPixel();
      state.view.x -= (ev.clientX - last[0]) * upp;
      state.view.y -= (ev.clientY - last[1]) * upp;
      last = [ev.clientX, ev.clientY];
      applyView();
    });
    const end = (ev) => {
      pointers.delete(ev.pointerId);
      if (pointers.size < 2) pinch = null;
      if (!pointers.size) svg.classList.remove("dragging");
      last = pointers.size ? [...pointers.values()][0] : null;
    };
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
    const center = () => [state.view.x + state.view.w / 2, state.view.y + state.view.h / 2];
    $("#z-in").addEventListener("click", () => zoomAt(1 / 1.4, ...center()));
    $("#z-out").addEventListener("click", () => zoomAt(1.4, ...center()));
    $("#z-reset").addEventListener("click", () => { state.view = { ...state.home }; applyView(); });
    window.addEventListener("resize", () => { applyView(); if (state.plan) renderCoverage(); });
  }

  // ------------------------------------------------------------------ tooltip
  function showTip(ev, s) {
    const fv = state.firstVisit.get(s.name);
    const visit = fv ? `<div>${fv.step === 0 ? "Start station" : `First reached at <b>${fmtClock(fv.clock)}</b> (step ${fv.step})`}</div>` : "";
    $("#tooltip").innerHTML = `<div><b>${esc(s.name)}</b></div><div>${codeChips(s)}</div>${visit}`;
    $("#tooltip").hidden = false;
    moveTip(ev);
  }
  function moveTip(ev) {
    const wrap = $(".map-wrap").getBoundingClientRect();
    const tip = $("#tooltip");
    let x = ev.clientX - wrap.left + 12, y = ev.clientY - wrap.top + 12;
    if (x + 220 > wrap.width) x -= 240;
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  }
  function hideTip() { $("#tooltip").hidden = true; }

  // ------------------------------------------------------------------ combobox
  function setupCombo() {
    const input = $("#start"), list = $("#start-list");
    let items = [], active = -1;
    const norm = (s) => s.toLowerCase().replace(/[^0-9a-z]/g, "");
    const close = () => { list.hidden = true; input.setAttribute("aria-expanded", "false"); active = -1; };
    const render = () => {
      const q = norm(input.value);
      const all = state.net ? state.net.stations : [];
      items = (q ? all.filter((s) => norm(s.name).includes(q) || s.codes.some(([, c]) => norm(c) === q || norm(c).startsWith(q)))
                 : all).slice(0, 40);
      items.sort((a, b) => (norm(a.name).startsWith(q) ? 0 : 1) - (norm(b.name).startsWith(q) ? 0 : 1));
      list.innerHTML = items.map((s, i) =>
        `<li role="option" id="opt-${i}" aria-selected="${i === active}" data-i="${i}"><span>${esc(s.name)}</span><span class="codes">${codeChips(s)}</span></li>`).join("");
      list.hidden = !items.length;
      input.setAttribute("aria-expanded", String(!list.hidden));
    };
    input.addEventListener("input", () => {
      active = -1;
      const exact = state.net && state.net.stations.some((st) => norm(st.name) === norm(input.value));
      if (exact) close(); else render();
    });
    input.addEventListener("focus", render);
    input.addEventListener("blur", () => setTimeout(close, 150));
    input.addEventListener("keydown", (ev) => {
      if (list.hidden && (ev.key === "ArrowDown" || ev.key === "ArrowUp")) render();
      if (ev.key === "ArrowDown") { active = Math.min(items.length - 1, active + 1); ev.preventDefault(); render(); }
      else if (ev.key === "ArrowUp") { active = Math.max(0, active - 1); ev.preventDefault(); render(); }
      else if (ev.key === "Enter" && active >= 0 && !list.hidden) { input.value = items[active].name; close(); }
      else if (ev.key === "Escape") close();
    });
    list.addEventListener("mousedown", (ev) => {
      const li = ev.target.closest("li");
      if (!li) return;
      input.value = items[+li.dataset.i].name;
      close();
      ev.preventDefault();
    });
    $("#quick-picks").innerHTML = QUICK.map((q) => `<button type="button" class="chip">${esc(q)}</button>`).join("");
    $("#quick-picks").addEventListener("click", (ev) => {
      const b = ev.target.closest(".chip");
      if (!b) return;
      input.value = b.textContent;
      solve();
    });
  }

  // ------------------------------------------------------------------ solving
  function setBusy(busy, title = "", detail = "") {
    $("#solve").disabled = busy;
    $("#status").hidden = !busy;
    if (busy) {
      $("#status-title").textContent = title;
      $("#status-detail").textContent = detail;
    }
  }

  function showError(msg) {
    $("#error").hidden = !msg;
    $("#error").textContent = msg || "";
  }

  async function solve() {
    const start = $("#start").value.trim();
    if (!start) { $("#start").focus(); return; }
    showError("");
    stopPlay();
    if (state.polling) clearTimeout(state.polling);
    const upcoming = $("#upcoming").checked;
    if (upcoming !== state.upcoming) await loadNetwork(upcoming);
    const body = {
      start,
      upcoming,
      reversal_penalty: $("#reversal").value === "" ? null : +$("#reversal").value,
      time_limit: +$("#time-limit").value || null,
      exact_seconds: +$("#exact").value || 0,
      live: $("#live").checked,
    };
    const lines = state.stationsByName.get(start)?.codes.length || 1;
    setBusy(true, `Planning from ${start}…`, body.live
      ? `Running ${lines} start line(s) × Guided Local Search portfolio`
      : "Checking precomputed routes");
    const params = new URLSearchParams({ start });
    if (upcoming) params.set("upcoming", "1");
    history.replaceState(null, "", `?${params}`);
    try {
      let job = await api("/api/solve", { method: "POST", body: JSON.stringify(body) });
      const t0 = Date.now();
      while (job.state === "queued" || job.state === "running") {
        const secs = Math.round((Date.now() - t0) / 1000);
        const lastMsg = job.progress && job.progress.length ? job.progress[job.progress.length - 1] : "";
        setBusy(true, `Optimising route from ${start}… ${secs}s`, lastMsg);
        await new Promise((r) => { state.polling = setTimeout(r, 700); });
        job = await api(`/api/jobs/${job.job_id}`);
      }
      if (job.state === "error") throw new Error(job.error);
      showPlan(job.result);
    } catch (err) {
      const sugg = err.body && err.body.suggestions && err.body.suggestions.length ? ` Try: ${err.body.suggestions.join(", ")}.` : "";
      showError(err.message + (err.message.includes("Did you mean") ? "" : sugg));
    } finally {
      setBusy(false);
    }
  }

  // ------------------------------------------------------------------ results
  function showPlan(plan) {
    state.plan = plan;
    const s = plan.summary;
    computeVisits(plan);

    $("#results").hidden = false;
    const stationObj = state.stationsByName.get(plan.start_station);
    $("#r-title").innerHTML = `${esc(plan.start_station)} ${stationObj ? codeChips(stationObj) : ""}`;
    $("#k-time").textContent = fmtHMc(s.total_minutes);
    $("#k-time").title = `${s.total_minutes} min`;
    $("#k-stations").textContent = `${s.stations_covered}/${s.stations_total}`;
    $("#k-transfers").textContent = s.transfers;
    $("#k-reversals").textContent = s.reversals;
    $("#f-start").innerHTML = `${pill(plan.start_line)} ${esc(lineName(plan.start_line))}`;
    $("#f-end").textContent = s.end_station;
    $("#f-train").textContent = `${fmtHM(s.in_train_minutes)} · ${s.stops_travelled} stops`;
    $("#f-change").textContent = `${fmtHM(s.transfer_minutes + s.reversal_minutes)}`;
    const o = plan.optimality;
    const note = $("#r-note");
    note.className = "note" + (o && o.proven_optimal ? " ok" : "");
    const model = plan.mode === "directional"
      ? `Direction-aware model, ${plan.reversal_penalty_min} min to turn back.`
      : "Pure two-layer platform model (turning back is free).";
    note.textContent = `${plan.optimality_note} ${model} Source: ${plan.solver.source}, ${plan.solver.method}.`;

    // start-line comparison
    const pl = plan.per_line;
    if (pl.length > 1) {
      // bars share a zero baseline; the delta text carries the (usually small) differences
      const max = Math.max(...pl.map((p) => p.minutes)), min = Math.min(...pl.map((p) => p.minutes));
      $("#per-line").innerHTML = `<div class="eyebrow">Start-line comparison (first train taken)</div>` + pl.map((p) => {
        const w = (100 * p.minutes) / max;
        const delta = p.minutes - min;
        const tag = p.best ? " · fastest" : ` <span class="pl-delta">+${+delta.toFixed(1)} min</span>`;
        return `<div class="pl-row${p.best ? " best" : ""}" title="${esc(p.line_name)}: ${p.minutes} min">${pill(p.line)}<div class="pl-bar"><span style="width:${w}%;background:${state.lineColor.get(p.line)}"></span></div><div class="pl-val">${esc(fmtHMc(p.minutes))}${tag}</div></div>`;
      }).join("");
    } else {
      $("#per-line").innerHTML = "";
    }

    renderItinerary();
    const slider = $("#p-slider");
    slider.max = plan.steps.length - 1;
    setStep(plan.steps.length - 1, false);
    if (window.matchMedia("(max-width: 1000px)").matches) $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // Arrival time of every first visit, from per-segment run times (exact, not end-of-leg).
  function computeVisits(plan) {
    const segMin = new Map();
    for (const g of state.net.segments) {
      segMin.set(`${g.line}|${g.a}|${g.b}`, g.minutes);
      segMin.set(`${g.line}|${g.b}|${g.a}`, g.minutes);
    }
    state.firstVisit.clear();
    state.visits = [{ t: 0, n: 1, station: plan.start_station, step: 0 }];
    state.firstVisit.set(plan.start_station, { step: 0, clock: 0 });
    plan.steps.forEach((st, i) => {
      if (st.kind !== "ride") return;
      let t = st.start_min;
      for (let k = 0; k + 1 < st.stations.length; k++) {
        t += segMin.get(`${st.line}|${st.stations[k]}|${st.stations[k + 1]}`) || 0;
        const name = st.stations[k + 1];
        if (!state.firstVisit.has(name)) {
          state.firstVisit.set(name, { step: i, clock: t });
          state.visits.push({ t, n: state.visits.length + 1, station: name, step: i });
        }
      }
    });
  }

  function stepAt(t) {
    const steps = state.plan.steps;
    let idx = 0;
    for (let i = 0; i < steps.length; i++) if (steps[i].start_min <= t + 1e-9) idx = i;
    while (idx > 0 && steps[idx].kind === "finish" && steps[idx].start_min > t) idx--;
    return idx;
  }

  function renderCoverage() {
    const card = $("#coverage-card"), box = $("#coverage");
    if (!state.plan) { card.hidden = true; return; }
    card.hidden = false;
    const W = box.clientWidth || 600, H = box.clientHeight || 180;
    const m = { l: 36, r: 112, t: 12, b: 24 };
    const total = state.plan.summary.total_minutes, nTot = state.plan.summary.stations_total;
    const hours = Math.max(1, Math.ceil(total / 60));
    const X = (t) => m.l + (t / (hours * 60)) * (W - m.l - m.r);
    const Y = (n) => m.t + (1 - n / nTot) * (H - m.t - m.b);
    box.innerHTML = "";
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Stations covered over time" }, box);
    const yTicks = [0, 25, 50, 75, 100, 125].filter((v) => v < nTot);
    for (const v of yTicks) {
      el("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), class: "grid" }, svg);
      el("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end", class: "axis-text" }, svg).textContent = v;
    }
    el("line", { x1: m.l, x2: W - m.r, y1: Y(nTot), y2: Y(nTot), class: "grid" }, svg);
    el("text", { x: m.l - 6, y: Y(nTot) + 4, "text-anchor": "end", class: "axis-text" }, svg).textContent = nTot;
    const step = hours > 12 || W < 520 ? 2 : 1;
    for (let h = 0; h <= hours; h += step) {
      el("text", { x: X(h * 60), y: H - 6, "text-anchor": "middle", class: "axis-text" }, svg).textContent = `${h}h`;
    }
    // step-after path through the first-visit times
    const v = state.visits;
    let d = `M${X(0)},${Y(v[0].n)}`;
    for (let i = 1; i < v.length; i++) d += ` H${X(v[i].t).toFixed(1)} V${Y(v[i].n).toFixed(1)}`;
    d += ` H${X(total).toFixed(1)}`;
    el("path", { d: `${d} V${Y(0)} H${X(0)} Z`, class: "series-area" }, svg);
    el("path", { d, class: "series-line" }, svg);
    el("circle", { cx: X(total), cy: Y(v[v.length - 1].n), r: 4, class: "end-dot" }, svg);
    el("text", { x: X(total) + 8, y: Y(v[v.length - 1].n) + 4, class: "end-label" }, svg).textContent =
      `${v[v.length - 1].n} in ${fmtHMc(total)}`;

    // current playback position
    const cur = state.plan.steps[state.step];
    const tNow = cur.end_min;
    const nNow = v.filter((p) => p.t <= tNow + 1e-9).length;
    el("line", { x1: X(tNow), x2: X(tNow), y1: m.t, y2: H - m.b, class: "now-line" }, svg);
    el("circle", { cx: X(tNow), cy: Y(nNow), r: 4, class: "now-dot" }, svg);

    // hover crosshair + tooltip, click to jump
    const cross = el("line", { x1: 0, x2: 0, y1: m.t, y2: H - m.b, class: "cross", visibility: "hidden" }, svg);
    const hit = el("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" }, svg);
    const tip = $("#coverage-tip");
    const tAt = (ev) => {
      const r = svg.getBoundingClientRect();
      const x = ((ev.clientX - r.left) / r.width) * W;
      return Math.max(0, Math.min(total, ((x - m.l) / (W - m.l - m.r)) * hours * 60));
    };
    hit.addEventListener("pointermove", (ev) => {
      const t = tAt(ev);
      const n = v.filter((p) => p.t <= t + 1e-9).length;
      const si = stepAt(t), st = state.plan.steps[si];
      cross.setAttribute("x1", X(t)); cross.setAttribute("x2", X(t)); cross.setAttribute("visibility", "visible");
      const what = st.kind === "ride" ? `${pill(st.line)} ${esc(st.station)} → ${esc(st.to_station)}`
        : st.kind === "transfer" ? `Changing ${pill(st.line)} → ${pill(st.to_line)} at ${esc(st.station)}`
        : st.kind === "reverse" ? `Turning back at ${esc(st.station)}` : esc(st.station);
      tip.innerHTML = `<b>${fmtClock(t)}</b> · ${n}/${nTot} stations<div>${what}</div>`;
      tip.hidden = false;
      const cr = $("#coverage-card").getBoundingClientRect(), r = svg.getBoundingClientRect();
      let left = r.left - cr.left + (X(t) / W) * r.width + 12;
      if (left + 240 > cr.width) left -= 260;
      tip.style.left = `${left}px`;
      tip.style.top = `${r.top - cr.top + 8}px`;
    });
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
    hit.addEventListener("click", (ev) => { stopPlay(); setStep(stepAt(tAt(ev)), true); });
  }

  function stepHTML(st, i) {
    const clock = `<div class="clock">${fmtClock(st.end_min)}</div>`;
    let body = "";
    if (st.kind === "start") {
      body = `<div class="step-kind">Start</div><div class="step-title">${esc(st.station)}</div><div class="step-sub">Board the ${pill(st.to_line)} ${esc(lineName(st.to_line))}</div>`;
    } else if (st.kind === "ride") {
      const newSet = new Set(st.new_stations);
      const via = st.stations.slice(1).map((n) => (newSet.has(n) ? `<b>${esc(n)}</b>` : esc(n))).join(" · ");
      body = `<div class="step-kind">Ride</div>
        <div class="step-title">${pill(st.line)} ${esc(st.station)} → ${esc(st.to_station)}</div>
        <div class="step-sub">${st.direction ? esc(st.direction) + " · " : ""}${st.stops} stop${st.stops === 1 ? "" : "s"} · ${fmtMin(st.minutes)}${st.new_stations.length ? ` · <span class="badge-new">+${st.new_stations.length} new</span>` : " · no new stations"} · ${st.covered_after} covered</div>
        <details class="stations-list"><summary>Stations</summary>${via}</details>`;
    } else if (st.kind === "transfer") {
      body = `<div class="step-kind">Transfer</div><div class="step-title">${esc(st.station)}: ${pill(st.line)} → ${pill(st.to_line)}</div><div class="step-sub">${fmtMin(st.minutes)}${st.note ? " · " + esc(st.note) : ""}</div>`;
    } else if (st.kind === "reverse") {
      body = `<div class="step-kind">Turn back</div><div class="step-title">${esc(st.station)} ${pill(st.line)}</div><div class="step-sub">${st.minutes ? fmtMin(st.minutes) + " · " : ""}alight and take the next train the other way</div>`;
    } else {
      body = `<div class="step-kind">Finish</div><div class="step-title">${esc(st.station)}</div><div class="step-sub">${esc(st.note)} in ${fmtHM(st.end_min)}</div>`;
    }
    return `<li data-i="${i}" tabindex="0">${clock}<div>${body}</div></li>`;
  }

  function renderItinerary() {
    const ol = $("#itinerary");
    ol.innerHTML = state.plan.steps.map(stepHTML).join("");
    ol.onclick = (ev) => {
      if (ev.target.closest("summary")) return;
      const li = ev.target.closest("li");
      if (li) { stopPlay(); setStep(+li.dataset.i, true); }
    };
    ol.onkeydown = (ev) => {
      const li = ev.target.closest("li");
      if (!li || ev.target !== li) return;
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); stopPlay(); setStep(+li.dataset.i, true); }
      else if (ev.key === "ArrowDown" && li.nextElementSibling) { ev.preventDefault(); li.nextElementSibling.focus(); }
      else if (ev.key === "ArrowUp" && li.previousElementSibling) { ev.preventDefault(); li.previousElementSibling.focus(); }
    };
  }

  // ------------------------------------------------------------------ playback
  function setStep(i, focusMap) {
    const steps = state.plan.steps;
    state.step = Math.max(0, Math.min(steps.length - 1, i));
    $("#p-slider").value = state.step;
    document.querySelectorAll("#itinerary li").forEach((li) => {
      const k = +li.dataset.i;
      li.classList.toggle("active", k === state.step);
      li.classList.toggle("future", k > state.step);
    });
    renderRoute();
    renderCoverage();
    if (focusMap) {
      const li = document.querySelector(`#itinerary li[data-i="${state.step}"]`);
      if (li && state.timer) li.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  function stopPlay() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
    $("#p-play").innerHTML = "&#9654;";
    $("#p-play").setAttribute("aria-label", "Play");
  }

  function togglePlay() {
    if (!state.plan) return;
    if (state.timer) { stopPlay(); return; }
    if (state.step >= state.plan.steps.length - 1) setStep(0, true);
    $("#p-play").innerHTML = "&#10074;&#10074;";
    $("#p-play").setAttribute("aria-label", "Pause");
    state.timer = setInterval(() => {
      if (state.step >= state.plan.steps.length - 1) { stopPlay(); return; }
      setStep(state.step + 1, true);
    }, 650);
  }

  function rideSegments(st) {
    const pts = [];
    for (let k = 0; k + 1 < st.stations.length; k++) {
      const [p1, p2] = segPoints(st.line, st.stations[k], st.stations[k + 1]);
      if (!pts.length) pts.push(p1);
      pts.push(p2);
    }
    return "M" + pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" L");
  }

  function renderRoute() {
    const svg = $("#map");
    const route = svg.querySelector("#route"), markers = svg.querySelector("#markers");
    route.innerHTML = "";
    markers.innerHTML = "";
    if (!state.plan) return;
    svg.classList.add("net-dim");
    const steps = state.plan.steps;
    const upto = state.step;
    const visited = new Set();
    for (let i = 0; i <= upto; i++) for (const n of steps[i].new_stations || []) visited.add(n);
    for (let i = 0; i <= upto; i++) {
      const st = steps[i];
      if (st.kind !== "ride") continue;
      const current = i === upto;
      if (current) {
        el("path", { d: rideSegments(st), class: "route-current", stroke: "var(--map-bg)", "stroke-width": 11, "vector-effect": "non-scaling-stroke", opacity: 0.9 }, route);
      }
      el("path", {
        d: rideSegments(st), class: current ? "route-current" : "route-seg", stroke: state.lineColor.get(st.line),
        "stroke-width": current ? 6.5 : 4.2, "vector-effect": "non-scaling-stroke",
      }, route);
    }
    svg.querySelectorAll("circle.st").forEach((c) => c.classList.toggle("visited", visited.has(c.dataset.name)));

    const startName = state.plan.start_station;
    const here = steps[upto].to_station || steps[upto].station;
    const done = upto === steps.length - 1;
    const mk = (name, cls, r) => {
      const [x, y] = P(name);
      const c = el("circle", { cx: x, cy: y, class: cls, "stroke-width": 2, "vector-effect": "non-scaling-stroke" }, markers);
      c.dataset.scale = r;
    };
    mk(startName, "marker-start", 7);
    if (done) mk(here, "marker-end", 7);
    else {
      const [x, y] = P(here);
      const p = el("circle", { cx: x, cy: y, class: "pulse", "stroke-width": 2, "vector-effect": "non-scaling-stroke" }, markers);
      p.dataset.scale = 7;
      mk(here, "marker-here", 6);
    }
    applyView();

    const cur = steps[upto];
    const desc = cur.kind === "ride" ? `${cur.line} ${cur.station} → ${cur.to_station}`
      : cur.kind === "transfer" ? `Change at ${cur.station}: ${cur.line} → ${cur.to_line}`
      : cur.kind === "reverse" ? `Turn back at ${cur.station}`
      : cur.kind === "start" ? `Start at ${cur.station}` : `Finish at ${cur.station}`;
    const prog = $("#map-progress");
    prog.hidden = false;
    prog.innerHTML = `<b>${fmtClock(cur.end_min)}</b> · step ${upto}/${steps.length - 1} · ${visited.size}/${state.plan.summary.stations_total} stations<br>${esc(desc)}`;
  }

  // ------------------------------------------------------------------ export
  function planText() {
    const p = state.plan, s = p.summary;
    const lines = [
      `All-stations MRT route from ${p.start_station}`,
      `Total ${s.total_hm} (${s.total_minutes} min), ${s.stations_covered}/${s.stations_total} stations, ${s.transfers} transfers, ${s.reversals} reversals`,
      `Start on ${p.start_line}, finish at ${s.end_station}`, "",
    ];
    for (const st of p.steps) {
      const c = fmtClock(st.end_min).padStart(5);
      if (st.kind === "start") lines.push(`${c}  START    ${st.station} (board ${st.to_line})`);
      else if (st.kind === "ride") lines.push(`${c}  RIDE ${st.line.padEnd(4)}${st.station} -> ${st.to_station} (${st.stops} stops, ${st.minutes} min, +${st.new_stations.length} new)`);
      else if (st.kind === "transfer") lines.push(`${c}  CHANGE   at ${st.station}: ${st.line} -> ${st.to_line} (${st.minutes} min)`);
      else if (st.kind === "reverse") lines.push(`${c}  TURN     at ${st.station} on ${st.line} (${st.minutes} min)`);
      else lines.push(`${c}  FINISH   at ${st.station}`);
    }
    return lines.join("\n");
  }

  function setupExport() {
    $("#copy-text").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(planText()); $("#copy-text").textContent = "Copied"; }
      catch { $("#copy-text").textContent = "Copy failed"; }
      setTimeout(() => { $("#copy-text").textContent = "Copy"; }, 1500);
    });
    $("#download-json").addEventListener("click", () => {
      const blob = new Blob([JSON.stringify(state.plan, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `mrt-route-${state.plan.start_station.replace(/\W+/g, "-").toLowerCase()}.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    });
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    setupCombo();
    setupMapInteraction();
    setupExport();
    $("#plan-form").addEventListener("submit", (ev) => { ev.preventDefault(); solve(); });
    $("#p-prev").addEventListener("click", () => { stopPlay(); if (state.plan) setStep(state.step - 1, true); });
    $("#p-next").addEventListener("click", () => { stopPlay(); if (state.plan) setStep(state.step + 1, true); });
    $("#p-play").addEventListener("click", togglePlay);
    $("#p-slider").addEventListener("input", (ev) => { stopPlay(); if (state.plan) setStep(+ev.target.value, false); });
    $("#upcoming").addEventListener("change", async (ev) => {
      state.plan = null;
      $("#results").hidden = true;
      $("#coverage-card").hidden = true;
      $("#map-progress").hidden = true;
      $("#map").classList.remove("net-dim");
      await loadNetwork(ev.target.checked);
    });

    const params = new URLSearchParams(location.search);
    const upcoming = params.get("upcoming") === "1";
    $("#upcoming").checked = upcoming;
    try {
      await loadNetwork(upcoming);
    } catch (err) {
      showError(`Could not load the network: ${err.message}`);
      return;
    }
    const start = params.get("start");
    if (start) { $("#start").value = start; solve(); }
  }

  boot();
})();
