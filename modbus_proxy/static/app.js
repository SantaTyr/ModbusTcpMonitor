/* Modbus TCP 监视器前端逻辑：SSE 实时帧 + 配置 + 详情 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const DIR = { up: "平台→设备", down: "设备→平台", gw: "网关→平台" };
  const DIR_CLASS = { up: "dir-up", down: "dir-down", gw: "dir-gw" };

  const S = {
    frames: [],
    seq: 0,
    paused: false,
    filter: { dir: "", fc: "", q: "", bad: false },
    configLoaded: false,
    maxKeep: 4000,
    maxRows: 800,
    running: false,
    state: null,
    pending: 0,
    autoScroll: true,
  };

  /* ----------------------------- 工具 ----------------------------- */
  const fmtBytes = (n) => {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
    return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
  };
  const fmtDur = (sec) => {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (d) return `${d}天${h}小时`;
    if (h) return `${h}小时${m}分`;
    if (m) return `${m}分${s}秒`;
    return `${s}秒`;
  };
  const hex2 = (v) => (v < 0 ? "" : v.toString(16).toUpperCase().padStart(2, "0"));
  const text = (node, value) => { node.textContent = value == null ? "" : String(value); };

  async function api(path) {
    const r = await fetch(path, { cache: "no-store" });
    if (!r.ok) throw new Error(path + " HTTP " + r.status);
    return r.json();
  }
  async function postJSON(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    let data;
    try { data = await r.json(); } catch { data = { ok: false, errors: ["响应解析失败"] }; }
    return data;
  }

  function toast(message, level) {
    const box = $("toast");
    const el = document.createElement("div");
    el.className = "item " + (level || "");
    el.textContent = message;
    box.appendChild(el);
    setTimeout(() => el.remove(), 4200);
    while (box.children.length > 4) box.firstChild.remove();
  }

  /* ----------------------------- 状态 ----------------------------- */
  function setPill(id, cls, value) {
    const el = $(id);
    el.className = "pill " + (cls || "");
    text(el.querySelector("em"), value);
  }

  function renderStats(st) {
    const stats = st.stats || {};
    const up = st.upstream || {};
    const rec = st.recording || {};
    const items = [
      ["监听", st.running ? st.listen : (st.listen_error ? "启动失败" : "已停止")],
      ["平台", st.client ? st.client.peer : "未连接"],
      ["设备", (up.state || "-") + (up.peer ? "  " + up.peer : "")],
      ["帧率", (stats.fps || 0) + " 帧/秒"],
      ["累计帧", `↑${stats.frames_up || 0} / ↓${stats.frames_down || 0}`],
      ["累计字节", `${fmtBytes(stats.bytes_up)} / ${fmtBytes(stats.bytes_down)}`],
      ["待应答", String(stats.pending || 0)],
      ["超时 / 错误", `${stats.timeouts || 0} / ${stats.errors || 0}`],
      ["记录", rec.enabled ? `${rec.frames || 0} 帧` : "已关闭"],
      ["运行时长", fmtDur(st.uptime)],
    ];
    if (st.listen_error) items.push(["监听错误", st.listen_error]);
    if (rec.error) items.push(["记录错误", rec.error]);
    const dl = $("stats");
    dl.textContent = "";
    for (const [k, v] of items) {
      const dt = document.createElement("dt"); text(dt, k);
      const dd = document.createElement("dd"); text(dd, v); dd.title = String(v);
      dl.append(dt, dd);
    }
    text($("rec-path"), rec.dir || (rec.error ? rec.error : "未开始记录"));
  }

  function applyState(st) {
    if (!st) return;
    S.state = st;
    S.running = !!st.running;
    text($("ver"), st.version ? "v" + st.version : "");
    text($("btn-power"), st.running ? "停止监听" : "开始监听");

    setPill("pill-listen",
      st.running ? "ok" : (st.listen_error ? "err" : ""),
      st.running ? st.listen : (st.listen_error ? "失败" : "已停止"));

    setPill("pill-client", st.client ? "ok" : "",
      st.client ? st.client.peer : "未连接");

    const up = st.upstream || {};
    const upState = up.state || "未连接";
    const upCls = upState === "已连接" ? "ok"
      : (upState === "连接失败" ? "err" : (upState === "连接中" || upState === "已断开" ? "warn" : ""));
    setPill("pill-device", upCls, upState + (up.peer ? " " + up.peer : ""));

    if (!S.configLoaded && st.config) {
      fillConfig(st.config);
      S.configLoaded = true;
    }
    renderStats(st);
  }

  function fillConfig(cfg) {
    const set = (id, v) => { const el = $(id); if (el) el.value = v == null ? "" : v; };
    set("c-listen_host", cfg.listen_host);
    set("c-listen_port", cfg.listen_port);
    set("c-device_host", cfg.device_host);
    set("c-device_port", cfg.device_port);
    set("c-connect_timeout", cfg.connect_timeout);
    set("c-response_timeout", cfg.response_timeout);
    set("c-idle_close", cfg.idle_close);
    set("c-timeout_policy", cfg.timeout_policy || "drop");
    $("c-record_enabled").checked = !!(cfg.record && cfg.record.enabled);
    text($("web-hint"), `网页端口 ${cfg.web_port}（修改需重启程序）`);
  }

  /* ----------------------------- 帧表 ----------------------------- */
  function matches(ev) {
    const f = S.filter;
    if (f.dir && ev.dir !== f.dir) return false;
    if (f.fc && String(ev.fc) !== f.fc) return false;
    if (f.bad && !ev.bad) return false;
    if (f.q) {
      const hay = (ev.hex + " " + ev.sum + " " + ev.tid + " " + (ev.fields || []).map((x) => x.join(" ")).join(" ")).toLowerCase();
      if (!hay.includes(f.q.toLowerCase())) return false;
    }
    return true;
  }

  function makeRow(ev) {
    const tr = document.createElement("tr");
    tr._ev = ev;
    if (ev.bad) tr.classList.add("bad");
    const cells = [
      ["", ev.ts],
      ["dir " + (DIR_CLASS[ev.dir] || ""), DIR[ev.dir] || ev.dir],
      ["", ev.tid || ""],
      ["", ev.fc >= 0 ? "0x" + hex2(ev.fc) : ""],
      ["sum", ev.sum || ""],
      ["rtt", ev.rtt != null ? ev.rtt.toFixed(1) : ""],
    ];
    for (const [cls, value] of cells) {
      const td = document.createElement("td");
      if (cls) td.className = cls;
      text(td, value);
      if (cls === "sum") td.title = value || "";
      tr.appendChild(td);
    }
    return tr;
  }

  function appendRow(ev) {
    const tb = $("tbody");
    tb.appendChild(makeRow(ev));
    while (tb.children.length > S.maxRows) tb.firstElementChild.remove();
    $("empty").hidden = true;
  }

  function rebuild() {
    const tb = $("tbody");
    tb.textContent = "";
    const list = S.frames.filter(matches).slice(-S.maxRows);
    const frag = document.createDocumentFragment();
    for (const ev of list) frag.appendChild(makeRow(ev));
    tb.appendChild(frag);
    $("empty").hidden = list.length > 0;
    S.pending = 0;
    updateCount();
    if (S.autoScroll) scrollBottom();
  }

  function pushFrame(ev) {
    ev._id = ++S.seq;
    S.frames.push(ev);
    if (S.frames.length > S.maxKeep) S.frames.splice(0, S.frames.length - S.maxKeep);
    if (!matches(ev)) { updateCount(); return; }
    if (S.paused) { S.pending++; updateCount(); return; }
    appendRow(ev);
    if (S.autoScroll) scrollBottom();
    updateCount();
  }

  function updateCount() {
    const total = S.frames.length;
    const shown = $("tbody").children.length;
    let msg = `共 ${total} 帧 · 显示 ${shown} 行`;
    if (S.paused) msg += ` · 已暂停${S.pending ? `（待显示 ${S.pending}）` : ""}`;
    text($("count"), msg);
  }

  function scrollBottom() {
    const wrap = $("table-wrap");
    wrap.scrollTop = wrap.scrollHeight;
    $("btn-jump").hidden = true;
  }

  /* ----------------------------- 详情 ----------------------------- */
  function selectRow(tr) {
    for (const row of $("tbody").children) row.classList.remove("sel");
    tr.classList.add("sel");
    openDetail(tr._ev);
  }

  function openDetail(ev) {
    $("detail").hidden = false;
    const bytes = (ev.hex || "").length / 2;
    text($("d-title"), `${DIR[ev.dir] || ev.dir} · ${ev.sum || ""}`);
    text($("d-sub"), `${ev.ts} · ${bytes} 字节 · TID ${ev.tid || "-"} · UID ${ev.uid >= 0 ? ev.uid : "-"}`);

    const dl = $("d-fields");
    dl.textContent = "";
    const rows = [["时间", ev.ts], ["方向", DIR[ev.dir] || ev.dir], ["长度", bytes + " 字节"]];
    for (const pair of ev.fields || []) rows.push(pair);
    if (ev.rtt != null) rows.push(["响应耗时", ev.rtt.toFixed(2) + " ms"]);
    for (const [k, v] of rows) {
      const dt = document.createElement("dt"); text(dt, k);
      const dd = document.createElement("dd"); text(dd, v);
      dl.append(dt, dd);
    }

    renderHex($("d-hex"), ev.hex || "");
    renderValues($("d-vals"), ev);
  }

  function renderHex(container, hex) {
    container.textContent = "";
    const bytes = hex.match(/../g) || [];
    if (!bytes.length) { text(container, "（无数据）"); return; }
    for (let i = 0; i < bytes.length; i += 16) {
      const chunk = bytes.slice(i, i + 16);
      const line = document.createElement("div");
      const off = document.createElement("span");
      off.className = "off";
      text(off, i.toString(16).toUpperCase().padStart(4, "0"));
      const body = document.createElement("span");
      text(body, "  " + chunk.join(" ").padEnd(47, " "));
      const ascii = document.createElement("span");
      ascii.className = "ascii";
      text(ascii, "  " + chunk.map((b) => {
        const c = parseInt(b, 16);
        return c >= 32 && c < 127 ? String.fromCharCode(c) : ".";
      }).join(""));
      line.append(off, body, ascii);
      container.appendChild(line);
    }
  }

  function renderValues(container, ev) {
    container.textContent = "";
    const vals = ev.vals || [];
    const bits = ev.bits || [];
    if (!vals.length && !bits.length) {
      const p = document.createElement("div");
      p.className = "hint";
      text(p, "该报文不含寄存器 / 位数据");
      container.appendChild(p);
      return;
    }
    const offset = (S.state && S.state.config && S.state.config.address_offset) || 0;
    const base = ev.vals_addr != null ? ev.vals_addr : ev.addr;
    const table = document.createElement("table");
    const thead = document.createElement("thead");
    const hr = document.createElement("tr");
    for (const h of ["地址", "HEX", "十进制 / 状态"]) {
      const th = document.createElement("th"); text(th, h); hr.appendChild(th);
    }
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    if (vals.length) {
      vals.forEach((v, i) => {
        const tr = document.createElement("tr");
        const addr = document.createElement("td");
        addr.className = "idx";
        text(addr, "0x" + ((base != null ? base + i : i) + offset).toString(16).toUpperCase().padStart(4, "0"));
        const hx = document.createElement("td");
        hx.className = "u";
        text(hx, "0x" + v.toString(16).toUpperCase().padStart(4, "0"));
        const dec = document.createElement("td");
        text(dec, String(v));
        tr.append(addr, hx, dec);
        tbody.appendChild(tr);
      });
    } else {
      bits.forEach((b, i) => {
        const tr = document.createElement("tr");
        const addr = document.createElement("td");
        addr.className = "idx";
        text(addr, "0x" + ((base != null ? base + i : i) + offset).toString(16).toUpperCase().padStart(4, "0"));
        const hx = document.createElement("td");
        text(hx, b ? "1" : "0");
        const dec = document.createElement("td");
        text(dec, b ? "ON" : "OFF");
        tr.append(addr, hx, dec);
        tbody.appendChild(tr);
      });
    }
    table.appendChild(tbody);
    container.appendChild(table);
  }

  /* ----------------------------- 交互 ----------------------------- */
  function bindUI() {
    $("btn-power").addEventListener("click", async () => {
      const action = S.running ? "stop" : "start";
      const res = await postJSON("/api/control", { action });
      if (res.state) applyState(res.state);
      if (res.ok) toast(action === "start" ? "已开始监听" : "已停止监听", "ok");
      else toast("启动失败，请检查监听端口是否被占用", "error");
    });

    $("btn-save").addEventListener("click", async () => {
      const msg = $("save-msg");
      const record = Object.assign({}, (S.state && S.state.config && S.state.config.record) || {}, {
        enabled: $("c-record_enabled").checked,
      });
      const body = {
        listen_host: $("c-listen_host").value.trim(),
        listen_port: Number($("c-listen_port").value),
        device_host: $("c-device_host").value.trim(),
        device_port: Number($("c-device_port").value),
        connect_timeout: Number($("c-connect_timeout").value),
        response_timeout: Number($("c-response_timeout").value),
        idle_close: Number($("c-idle_close").value),
        timeout_policy: $("c-timeout_policy").value,
        record,
      };
      msg.className = "msg";
      text(msg, "正在应用…");
      try {
        const res = await postJSON("/api/config", body);
        if (!res.ok) {
          msg.className = "msg err";
          text(msg, (res.errors || ["保存失败"]).join("；"));
          return;
        }
        S.configLoaded = false;
        applyState(res.state);
        if (res.restart_required) {
          msg.className = "msg warn";
          text(msg, "已保存。网页端口修改需重启程序生效。");
        } else {
          msg.className = "msg ok";
          text(msg, "已保存并应用。");
        }
      } catch (err) {
        msg.className = "msg err";
        text(msg, "保存失败：" + err.message);
      }
    });

    $("btn-open").addEventListener("click", async () => {
      const res = await postJSON("/api/open-folder", {});
      if (!res.ok) toast("打开目录失败：" + (res.errors || []).join("；"), "error");
    });

    $("btn-clear").addEventListener("click", async () => {
      S.frames = [];
      S.pending = 0;
      rebuild();
      await postJSON("/api/clear", {});
      toast("已清空显示", "ok");
    });

    $("btn-pause").addEventListener("click", () => {
      S.paused = !S.paused;
      $("btn-pause").classList.toggle("active", S.paused);
      text($("btn-pause"), S.paused ? "继续显示" : "暂停显示");
      if (!S.paused) rebuild();
      else updateCount();
    });

    $("btn-export").addEventListener("click", () => {
      const rows = S.frames.filter(matches);
      if (!rows.length) { toast("没有可导出的数据", "warn"); return; }
      const esc = (v) => '"' + String(v == null ? "" : v).replace(/"/g, '""') + '"';
      const lines = ["时间,方向,TID,功能码,摘要,原始数据(HEX)"];
      for (const ev of rows) {
        lines.push([
          esc(ev.ts), esc(DIR[ev.dir] || ev.dir), esc(ev.tid || ""),
          esc(ev.fc >= 0 ? "0x" + hex2(ev.fc) : ""), esc(ev.sum || ""), esc(ev.hex || ""),
        ].join(","));
      }
      const blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
      const a = document.createElement("a");
      const d = new Date();
      const stamp = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}_${String(d.getHours()).padStart(2, "0")}${String(d.getMinutes()).padStart(2, "0")}${String(d.getSeconds()).padStart(2, "0")}`;
      a.href = URL.createObjectURL(blob);
      a.download = `modbus_frames_${stamp}.csv`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 3000);
    });

    $("btn-jump").addEventListener("click", () => {
      S.autoScroll = true;
      scrollBottom();
    });

    $("btn-detail-close").addEventListener("click", () => {
      $("detail").hidden = true;
      for (const row of $("tbody").children) row.classList.remove("sel");
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") $("btn-detail-close").click();
    });

    $("tbody").addEventListener("click", (e) => {
      const tr = e.target.closest("tr");
      if (tr && tr._ev) selectRow(tr);
    });

    const rebind = () => { S.filter = readFilter(); rebuild(); };
    $("f-dir").addEventListener("change", rebind);
    $("f-fc").addEventListener("change", rebind);
    $("f-bad").addEventListener("change", rebind);
    let timer = null;
    $("f-q").addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(rebind, 180);
    });

    $("table-wrap").addEventListener("scroll", () => {
      const wrap = $("table-wrap");
      const atBottom = wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 60;
      S.autoScroll = atBottom;
      $("btn-jump").hidden = atBottom;
    });
  }

  function readFilter() {
    return {
      dir: $("f-dir").value,
      fc: $("f-fc").value,
      bad: $("f-bad").checked,
      q: $("f-q").value.trim(),
    };
  }

  /* ----------------------------- 启动 ----------------------------- */
  function connectSSE() {
    const es = new EventSource("/api/events");
    es.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      if (msg.type === "frame") pushFrame(msg);
      else if (msg.type === "state") applyState(msg.state);
      else if (msg.type === "sys" && (msg.level === "warn" || msg.level === "error")) {
        toast(msg.text, msg.level);
      }
    };
    es.onerror = () => { /* EventSource 会自动重连 */ };
  }

  async function boot() {
    bindUI();
    try {
      const st = await api("/api/state");
      applyState(st);
    } catch (err) {
      toast("无法获取运行状态：" + err.message, "error");
    }
    try {
      const data = await api("/api/frames?limit=600");
      for (const ev of data.frames || []) {
        ev._id = ++S.seq;
        S.frames.push(ev);
      }
      rebuild();
    } catch (err) {
      toast("加载历史帧失败：" + err.message, "error");
    }
    connectSSE();
  }

  boot();
})();
