/* 小米爱听 XiaoMi-AiTing —— 前端逻辑（原生 JS，无构建依赖） */
(function () {
  "use strict";

  const API = "/api/aiting";
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  // 音量按钮每次加减的幅度（老板要求：每次 1）
  const VOL_STEP = 1;

  // 用户刚调过音量后的「保护期」。这段时间内轮询回来的音量一律不采信。
  // 原因：连续点「+1」时，每一次点击都会触发一次 700ms 后执行的 pollPlaying；
  // 这些请求是并发的，先发出的那个可能带着**更旧**的音量回来，把本地刚设好的
  // 基准覆盖掉 —— 表现就是「连点三下，第三下不动了」。
  // 服务端音量缓存 TTL 是 5 秒，这里取 4 秒，保证本地值优先。
  const VOL_LOCAL_GRACE_MS = 4000;

  const S = {
    did: "",
    devices: [],
    radio: { board: null, region: null, category: null, keyword: "", fav: false,
             sort: "hot", page: 1, limit: 60, total: 0, items: [] },
    music: { playlist: "", keyword: "", page: 1, limit: 200, total: 0, items: [] },
    netmusic: { items: [], local: [], searched: false },
    tree: [],
    playlists: [],
    playing: false,
    curName: "",
    sleep: null,
    volDragging: false,   // 用户正在拖音量滑块时，轮询不要回写滑块
    volLocalTs: 0,        // 本地最后一次主动设音量的时刻（见 VOL_LOCAL_GRACE_MS）
    normOn: true,         // 响度均衡开关
    _sleepDone: false,    // 睡眠定时器结束提示只弹一次
    _tick: 0,
  };

  /* ---------------------------------------------------------- 工具 */
  function toast(msg, type) {
    const el = document.createElement("div");
    el.className = "toast " + (type || "");
    el.textContent = msg;
    $("#toasts").appendChild(el);
    // 长提示（例如「音源连不上」）多留一会儿，别让人来不及看
    const ms = Math.min(9000, 2600 + Math.max(0, msg.length - 18) * 90);
    setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 250); }, ms);
  }

  async function api(path, opt) {
    opt = opt || {};
    opt.headers = Object.assign({ "Content-Type": "application/json" }, opt.headers || {});
    if (opt.body && typeof opt.body !== "string") opt.body = JSON.stringify(opt.body);
    const r = await fetch(API + path, opt);
    if (r.status === 401) { toast("需要登录：请输入 HTTP 认证账号密码", "err"); throw new Error("401"); }
    const t = await r.text();
    try { return JSON.parse(t); } catch (e) { return { ret: "Raw", data: t }; }
  }

  function fmtTime(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const m = Math.floor(sec / 60), s = sec % 60;
    return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
  }
  function fmtUp(sec) {
    sec = Math.floor(sec || 0);
    if (sec < 3600) return Math.floor(sec / 60) + "分";
    if (sec < 86400) return (sec / 3600).toFixed(1) + "时";
    return Math.floor(sec / 86400) + "天";
  }
  function fmtSize(b) {
    if (!b) return "-";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return b.toFixed(1) + u[i];
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* ---------------------------------------------------------- 主题 */
  function initTheme() {
    const saved = localStorage.getItem("aiting-theme") || "light";
    document.documentElement.setAttribute("data-theme", saved);
    $("#themeBtn").textContent = saved === "dark" ? "☀️" : "🌙";
    $("#themeBtn").onclick = () => {
      const cur = document.documentElement.getAttribute("data-theme");
      const nxt = cur === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", nxt);
      localStorage.setItem("aiting-theme", nxt);
      $("#themeBtn").textContent = nxt === "dark" ? "☀️" : "🌙";
    };
  }

  /* ---------------------------------------------------------- 导航 */
  function go(page) {
    $$(".page").forEach(p => p.classList.toggle("on", p.id === "page-" + page));
    $$(".nav-item").forEach(b => b.classList.toggle("on", b.dataset.page === page));
    if (page === "fav") loadFav();
    if (page === "devices") renderDevices();
    if (page === "system") { loadSysInfo(); loadConfig(); }
    if (page === "music" && !S.playlists.length) loadPlaylists();
    if (page === "netmusic") loadNetLocal();
    $("#main").scrollTop = 0;
  }
  function initNav() {
    $$(".nav-item[data-page]").forEach(b => b.onclick = () => go(b.dataset.page));
    $("#classicBtn").onclick = () => window.open("/static/default/index.html", "_blank");
  }

  /* ---------------------------------------------------------- 状态 */
  async function loadStatus() {
    try {
      const d = await api("/status");
      if (d.ret !== "OK") return;
      S.devices = d.devices || [];
      renderDevSelect();
      $("#sDev").textContent = S.devices.length;
      $("#sSong").innerHTML = (d.music.songs || 0).toLocaleString() + '<small>首</small>';
      $("#sRadio").innerHTML = (d.radio.total || 0).toLocaleString() + '<small>个</small>';
      $("#rTotal").textContent = (d.radio.total || 0).toLocaleString();
      $("#sUp").textContent = fmtUp(d.uptime);
      if (!S.did && S.devices.length) { S.did = S.devices[0].did; $("#devSel").value = S.did; }
      if (d.need_login) toast("小米账号尚未登录，请到「系统与设置」配置", "err");
    } catch (e) { /* ignore */ }
  }

  function renderDevSelect() {
    const sel = $("#devSel");
    sel.innerHTML = S.devices.length
      ? S.devices.map(d => `<option value="${esc(d.did)}">${esc(d.name)}</option>`).join("")
      : '<option value="">未发现音箱</option>';
    if (S.did) sel.value = S.did;
  }

  /* ---------------------------------------------------------- 播放状态轮询 */
  async function pollPlaying() {
    if (!S.did) return;
    try {
      const d = await api("/player/now?did=" + encodeURIComponent(S.did));
      if (d.ret !== "OK") return;
      S.playing = !!d.is_playing;
      S.curName = d.cur_music || "";
      $("#pName").textContent = d.cur_music || "未在播放";
      $("#pList").textContent = d.cur_playlist || "—";
      $("#nowName").textContent = d.cur_music || "未在播放";
      $("#pToggle").textContent = d.is_playing ? "⏸" : "▶";
      const dur = d.duration || 0, off = Math.min(d.offset || 0, dur || 0);
      $("#pCur").textContent = fmtTime(off);
      $("#pDur").textContent = dur ? fmtTime(dur) : "LIVE";
      $("#pFill").style.width = dur ? (off / dur * 100).toFixed(1) + "%" : "0%";
      // 音量必须同步回滑块。
      // 之前滑块永远停在 HTML 默认值上，用户一拖就把音量顶到滑块位置（听感上就是突然炸响）。
      const t = S.devices.find(x => x.did === S.did);
      const v = (d.volume != null) ? d.volume : (t && t.volume != null ? t.volume : null);
      // 用户刚动过音量时以本地为准，别让迟到的轮询结果把基准改回去。
      const localQuiet = (Date.now() - (S.volLocalTs || 0)) > VOL_LOCAL_GRACE_MS;
      if (v != null && localQuiet) {
        // 关键：必须把真实音量回写到设备列表。
        // currentVolume() 优先读 S.devices，如果不回写，它就会一直返回页面
        // 加载那一刻的旧值 —— 表现为「连点好几下 +1，音量只加 1」。
        if (t) t.volume = v;
        $("#volTxt").textContent = v;
        $("#volNow").textContent = v;
        if (!S.volDragging) syncVolUI(v);
      }
      if (t && t.play_type != null) $("#playType").value = String(t.play_type);
      // 睡眠模式倒计时（每 5 秒刷新一次即可）
      if (++S._tick % 5 === 0) loadSleep();
    } catch (e) { /* ignore */ }
  }

  /* ---------------------------------------------------------- 播放控制 */
  async function cmd(c, extra) {
    if (!S.did) { toast("未选择音箱设备", "err"); return; }
    const d = await api("/player/command", {
      method: "POST", body: Object.assign({ did: S.did, cmd: c }, extra || {})
    });
    if (d.ret !== "OK") toast(d.msg || "操作失败", "err");
    else setTimeout(pollPlaying, 700);
  }

  /** 把真实音量同步到两个滑块（用户正在拖动时不要调用） */
  function syncVolUI(v) {
    const a = $("#pVol"), b = $("#volRange");
    if (a && +a.value !== +v) a.value = v;
    if (b && +b.value !== +v) b.value = v;
  }

  /** 当前已知的音箱音量（优先取设备列表里的真实值） */
  function currentVolume() {
    const t = S.devices.find(x => x.did === S.did);
    if (t && t.volume != null) return +t.volume;
    const p = parseInt($("#pVol").value, 10);
    return isNaN(p) ? 0 : p;
  }

  /** 设定绝对音量（0-100）。
   *  睡眠模式进行中时，同步把渐弱基准重置到新音量，
   *  否则 20 秒后定时器会把音量拉回旧值，用户会觉得「音量不受控」。 */
  async function setVolume(v) {
    v = Math.max(0, Math.min(100, Math.round(v)));
    // 立刻把新值写回本地基准：连点「+1」时每次都要基于刚设过的值，
    // 否则会一直拿旧值去算（点三下只加 1）。
    // 同时打上时间戳，让迟到的轮询结果在保护期内不覆盖它。
    S.volLocalTs = Date.now();
    const t = S.devices.find(x => x.did === S.did);
    if (t) t.volume = v;
    $("#volTxt").textContent = v;
    $("#volNow").textContent = v;
    syncVolUI(v);
    await cmd("volume", { volume: v });
    if (S.sleep && S.sleep.did) {
      try {
        await api("/sleep/rebase", {
          method: "POST", body: { did: S.did, volume: v }
        });
      } catch (e) { /* ignore */ }
      loadSleep();
    }
  }

  /** 相对微调音量：安全，不会像拖滑块那样一下顶到高位 */
  async function nudgeVolume(delta) {
    await setVolume(currentVolume() + delta);
  }

  function bindVolumeSlider(el) {
    if (!el) return;
    const mark = () => { S.volDragging = true; };
    const unmark = () => { S.volDragging = false; };
    el.addEventListener("pointerdown", mark);
    el.addEventListener("mousedown", mark);
    el.addEventListener("touchstart", mark, { passive: true });
    el.addEventListener("pointerup", unmark);
    el.addEventListener("mouseup", unmark);
    el.addEventListener("touchend", unmark);
    el.addEventListener("blur", unmark);
    el.addEventListener("keyup", unmark);
    el.oninput = (e) => {
      $("#volTxt").textContent = e.target.value;
      $("#volNow").textContent = e.target.value;
    };
    el.onchange = (e) => {
      unmark();
      setVolume(+e.target.value);
    };
  }

  function initPlayerBar() {
    $("#pToggle").onclick = () => cmd(S.playing ? "stop" : "play", {});
    $("#pPrev").onclick = () => cmd("prev");
    $("#pNext").onclick = () => cmd("next");
    $("#pBar").onclick = (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      const pct = (e.clientX - r.left) / r.width;
      const durTxt = $("#pDur").textContent;
      if (durTxt === "LIVE") { toast("电台直播无法拖动进度"); return; }
      const parts = durTxt.split(":").map(Number);
      const dur = parts[0] * 60 + parts[1];
      cmd("position", { position: Math.floor(dur * pct) });
    };
    // 音量：滑块 + 微调按钮
    bindVolumeSlider($("#pVol"));
    bindVolumeSlider($("#volRange"));
    const up = $("#pVolUp"), down = $("#pVolDown");
    if (up) up.onclick = () => nudgeVolume(VOL_STEP);
    if (down) down.onclick = () => nudgeVolume(-VOL_STEP);
    const up2 = $("#volUp"), down2 = $("#volDown");
    if (up2) up2.onclick = () => nudgeVolume(VOL_STEP);
    if (down2) down2.onclick = () => nudgeVolume(-VOL_STEP);

    $("#playType").onchange = (e) => cmd("play_type", { play_type: +e.target.value });
    $$("[data-cmd]").forEach(b => b.onclick = () => cmd(b.dataset.cmd, {}));
  }

  /* ---------------------------------------------------------- 响度均衡 */

  async function loadNormalize() {
    try {
      const d = await api("/radio/normalize");
      if (d.ret !== "OK") return;
      S.normOn = !!d.settings.normalize;
      const sel = $("#normalizeSel");
      if (sel) sel.value = S.normOn ? "1" : "0";
    } catch (e) { /* ignore */ }
  }

  function initNormalizeUI() {
    const sel = $("#normalizeSel");
    if (!sel) return;
    sel.onchange = async (e) => {
      const on = e.target.value === "1";
      const d = await api("/radio/normalize", {
        method: "POST", body: { enabled: on }
      });
      if (d.ret === "OK") {
        S.normOn = on;
        toast(on ? "已开启响度均衡：各电台音量拉平" : "已关闭响度均衡");
      } else {
        toast(d.msg || "设置失败", "err");
      }
    };
  }

  /* ---------------------------------------------------------- 电台 */
  async function loadTree() {
    const d = await api("/radio/tree");
    S.tree = d.tree || [];
    renderBoards();
  }

  function renderBoards() {
    const wrap = $("#rBoards");
    wrap.innerHTML = S.tree.map(b =>
      `<div class="chip ${S.radio.board === b.board ? "on" : ""}" data-board="${esc(b.board)}">
        ${b.icon} ${esc(b.board)}<span class="n">${b.count}</span></div>`).join("");
    $$(".chip", wrap).forEach(c => c.onclick = () => {
      S.radio.board = c.dataset.board; S.radio.region = null; S.radio.category = null;
      S.radio.page = 1; renderBoards(); renderRegions(); renderCats(); loadRadio();
    });
    renderRegions();
  }

  function renderRegions() {
    const wrap = $("#rRegions");
    const b = S.tree.find(x => x.board === S.radio.board);
    if (!b) { wrap.innerHTML = ""; return; }
    wrap.innerHTML = `<div class="chip ${!S.radio.region ? "on" : ""}" data-reg="">全部地区</div>` +
      b.regions.map(r => `<div class="chip ${S.radio.region === r.name ? "on" : ""}"
        data-reg="${esc(r.name)}">${esc(r.name)}<span class="n">${r.count}</span></div>`).join("");
    $$(".chip", wrap).forEach(c => c.onclick = () => {
      S.radio.region = c.dataset.reg || null; S.radio.category = null;
      S.radio.page = 1; renderRegions(); renderCats(); loadRadio();
    });
    renderCats();
  }

  function renderCats() {
    const wrap = $("#rCats");
    const b = S.tree.find(x => x.board === S.radio.board);
    if (!b) { wrap.innerHTML = ""; return; }
    let cats = {};
    b.regions.forEach(r => {
      if (S.radio.region && r.name !== S.radio.region) return;
      r.categories.forEach(c => cats[c.name] = (cats[c.name] || 0) + c.count);
    });
    const arr = Object.entries(cats).sort((a, b2) => b2[1] - a[1]);
    wrap.innerHTML = `<div class="chip ${!S.radio.category ? "on" : ""}" data-cat="">全部类型</div>` +
      arr.map(([n, c]) => `<div class="chip ${S.radio.category === n ? "on" : ""}"
        data-cat="${esc(n)}">${esc(n)}<span class="n">${c}</span></div>`).join("");
    $$(".chip", wrap).forEach(c => c.onclick = () => {
      S.radio.category = c.dataset.cat || null; S.radio.page = 1;
      renderCats(); loadRadio();
    });
  }

  async function loadRadio(append) {
    if (!append) { S.radio.page = 1; $("#radioList").innerHTML = '<div class="loading"><span class="spin"></span>加载中…</div>'; }
    const p = new URLSearchParams({
      page: S.radio.page, limit: S.radio.limit, sort: S.radio.sort,
    });
    if (S.radio.board) p.set("board", S.radio.board);
    if (S.radio.region) p.set("region", S.radio.region);
    if (S.radio.category) p.set("category", S.radio.category);
    if (S.radio.keyword) p.set("keyword", S.radio.keyword);
    if (S.radio.fav) p.set("fav", "true");

    const d = await api("/radio/list?" + p.toString());
    const items = d.items || [];
    S.radio.total = d.total || 0;
    if (!append) S.radio.items = items; else S.radio.items = S.radio.items.concat(items);
    renderRadio();
    $("#rCount").textContent = `共 ${S.radio.total} 个电台，已显示 ${S.radio.items.length} 个`;
    $("#rMore").style.display = S.radio.items.length < S.radio.total ? "" : "none";
  }

  function renderRadio() {
    const box = $("#radioList");
    if (!S.radio.items.length) {
      box.innerHTML = '<div class="empty"><div class="big">📻</div>没有找到电台</div>';
      return;
    }
    box.innerHTML = S.radio.items.map((s, i) => {
      const badge = s.ok === false ? '<span class="tag err">不可用</span>'
        : s.source === "custom" ? '<span class="tag brand">自定义</span>' : "";
      return `<div class="row" data-id="${esc(s.id)}">
        <div class="idx">${i + 1}</div>
        <div class="nm"><div>${esc(s.name)}</div>
          <div class="sub">${esc(s.region || "")} · ${esc(s.category || "")} ${badge}</div></div>
        <div class="star ${s.fav ? "on" : ""}" data-fav="${esc(s.id)}">★</div>
        <div class="acts">
          <button class="btn sm" data-play="${esc(s.id)}">▶ 播放</button>
        </div></div>`;
    }).join("");

    $$(".row", box).forEach(r => {
      r.onclick = (e) => {
        if (e.target.dataset.fav !== undefined && e.target.dataset.fav !== "") return;
        playRadio(r.dataset.id);
      };
    });
    $$("[data-fav]", box).forEach(st => st.onclick = async (e) => {
      e.stopPropagation();
      const d = await api("/radio/fav", { method: "POST", body: { id: st.dataset.fav } });
      if (d.ret === "OK") { st.classList.toggle("on", d.fav); toast(d.fav ? "已收藏" : "已取消收藏"); }
    });
  }

  async function playRadio(id) {
    if (!S.did) { toast("请先选择音箱", "err"); return; }
    // 把当前列表作为播放队列，支持上一首/下一首
    const queue = S.radio.items.map(s => s.id).slice(0, 200);
    // 播前会做音源连通性探测，可能要几秒，先给个反馈
    toast("正在连接电台…");
    const d = await api("/radio/play", { method: "POST", body: { did: S.did, id: id, queue: queue } });
    if (d.ret === "OK") { toast("正在播放：" + d.name, "ok"); setTimeout(pollPlaying, 900); }
    else toast(d.msg || "播放失败", "err");
  }

  function initRadioUI() {
    let tm = null;
    $("#rSearch").oninput = (e) => {
      clearTimeout(tm);
      tm = setTimeout(() => { S.radio.keyword = e.target.value.trim(); loadRadio(); }, 320);
    };
    $("#rSort").onchange = (e) => { S.radio.sort = e.target.value; loadRadio(); };
    $("#rFavOnly").onclick = (e) => {
      S.radio.fav = !S.radio.fav;
      e.target.classList.toggle("primary", S.radio.fav);
      loadRadio();
    };
    $("#rMore").onclick = () => { S.radio.page++; loadRadio(true); };
    $("#rCheckAll").onclick = async (e) => {
      e.target.disabled = true;
      e.target.innerHTML = '<span class="spin"></span>检测中…';
      const d = await api("/radio/check", { method: "POST", body: {
        board: S.radio.board, region: S.radio.region, category: S.radio.category, limit: 60 } });
      e.target.disabled = false; e.target.textContent = "🩺 批量检测连通性";
      if (d.ret === "OK") { toast(`检测 ${d.checked} 个，可用 ${d.ok} 个`, "ok"); loadRadio(); }
      else toast("检测失败", "err");
    };
    $("#rPlayAll").onclick = () => {
      if (!S.radio.items.length) { toast("列表为空"); return; }
      playRadio(S.radio.items[0].id);
      toast("已从列表第一首开始播放");
    };
    $("#rAddBtn").onclick = showAddStation;
    $("#rOnlineBtn").onclick = showOnline;
  }

  /* ---------------------------------------------------------- 自定义 / 在线 */
  function openModal(html) {
    $("#modal").innerHTML = html;
    $("#mask").classList.add("on");
  }
  function closeModal() { $("#mask").classList.remove("on"); }

  function showAddStation() {
    openModal(`<h3>＋ 新增自定义电台</h3>
      <label class="f"><span>电台名称</span><input type="text" id="nsName" placeholder="例如：我家喜欢的电台" /></label>
      <label class="f"><span>播放地址（http/https，支持 mp3 / m3u8 流）</span>
        <input type="text" id="nsUrl" placeholder="http://example.com/live.mp3" /></label>
      <label class="f"><span>分类</span><input type="text" id="nsCat" value="自定义" /></label>
      <label class="f"><span>地区</span><input type="text" id="nsReg" value="自定义" /></label>
      <div class="foot"><button class="btn" id="nsCancel">取消</button>
        <button class="btn primary" id="nsOk">保存</button></div>`);
    $("#nsCancel").onclick = closeModal;
    $("#nsOk").onclick = async () => {
      const d = await api("/radio/custom", { method: "POST", body: {
        name: $("#nsName").value, url: $("#nsUrl").value,
        category: $("#nsCat").value, region: $("#nsReg").value } });
      if (d.ret === "OK") { toast("已添加", "ok"); closeModal(); loadRadio(); loadTree(); }
      else toast(d.msg || "添加失败", "err");
    };
  }

  async function showOnline() {
    openModal(`<h3>☁ 在线电台库</h3>
      <p style="color:var(--text-2);font-size:13px;margin-top:-6px">
        从公开电台数据库检索，选中后加入「我的电台」。</p>
      <label class="f"><span>关键词（留空则列出该国热门）</span>
        <input type="text" id="onKw" placeholder="例如：音乐 / 新闻" /></label>
      <div class="foot"><button class="btn" id="onCancel">关闭</button>
        <button class="btn primary" id="onSearch">检索</button></div>
      <div class="list" id="onList" style="max-height:340px;overflow:auto;margin-top:12px"></div>`);
    $("#onCancel").onclick = closeModal;
    $("#onSearch").onclick = async () => {
      $("#onList").innerHTML = '<div class="loading"><span class="spin"></span>检索中…</div>';
      const d = await api("/radio/online?keyword=" + encodeURIComponent($("#onKw").value) + "&limit=50");
      if (d.ret !== "OK") { $("#onList").innerHTML = '<div class="empty">检索失败</div>'; return; }
      $("#onList").innerHTML = d.items.map((s, i) => `<div class="row">
        <div class="idx">${i + 1}</div>
        <div class="nm"><div>${esc(s.name)}</div><div class="sub">${esc(s.tags || s.country)}</div></div>
        <button class="btn sm" data-add="${i}">＋ 添加</button></div>`).join("") ||
        '<div class="empty">没有结果</div>';
      $$("[data-add]", $("#onList")).forEach(b => b.onclick = async () => {
        const s = d.items[+b.dataset.add];
        const r = await api("/radio/custom", { method: "POST", body: {
          name: s.name, url: s.url, category: "在线导入", region: s.country || "在线" } });
        toast(r.ret === "OK" ? "已添加：" + s.name : (r.msg || "失败"), r.ret === "OK" ? "ok" : "err");
        if (r.ret === "OK") loadTree();
      });
    };
  }

  /* ---------------------------------------------------------- 收藏页 */
  async function loadFav() {
    const box = $("#favList");
    box.innerHTML = '<div class="loading"><span class="spin"></span>加载中…</div>';
    const [f, c] = await Promise.all([
      api("/radio/list?fav=true&limit=500"),
      api("/radio/list?source=custom&limit=500"),
    ]);
    const items = (f.items || []).concat((c.items || []).filter(
      x => !(f.items || []).some(y => y.id === x.id)));
    if (!items.length) { box.innerHTML = '<div class="empty"><div class="big">⭐</div>还没有收藏，去「网络收音机」逛逛</div>'; return; }
    box.innerHTML = items.map((s, i) => `<div class="row" data-id="${esc(s.id)}">
      <div class="idx">${i + 1}</div>
      <div class="nm"><div>${esc(s.name)}</div>
        <div class="sub">${esc(s.region)} · ${esc(s.category)}
          ${s.source === "custom" ? '<span class="tag brand">自定义</span>' : ""}</div></div>
      <div class="star on" data-fav="${esc(s.id)}">★</div>
      <div class="acts"><button class="btn sm" data-play="${esc(s.id)}">▶ 播放</button>
        ${s.source === "custom" ? `<button class="btn sm" data-del="${esc(s.id)}">删除</button>` : ""}</div>
    </div>`).join("");
    $$(".row", box).forEach(r => r.onclick = (e) => {
      if (e.target.dataset.fav || e.target.dataset.del) return;
      playRadio(r.dataset.id);
    });
    $$("[data-fav]", box).forEach(st => st.onclick = async (e) => {
      e.stopPropagation();
      await api("/radio/fav", { method: "POST", body: { id: st.dataset.fav } });
      loadFav();
    });
    $$("[data-del]", box).forEach(b => b.onclick = async (e) => {
      e.stopPropagation();
      const d = await api("/radio/custom/" + b.dataset.del, { method: "DELETE" });
      toast(d.ret === "OK" ? "已删除" : (d.msg || "失败"), d.ret === "OK" ? "ok" : "err");
      if (d.ret === "OK") { loadFav(); loadTree(); }
    });
  }

  /* ---------------------------------------------------------- 本地音乐 */
  async function loadPlaylists() {
    const d = await api("/music/playlists");
    S.playlists = d.playlists || [];
    const wrap = $("#mPlaylists");
    if (!S.playlists.length) { wrap.innerHTML = ""; return; }
    wrap.innerHTML = S.playlists.map(p => `<div class="chip ${S.music.playlist === p.name ? "on" : ""}"
      data-pl="${esc(p.name)}">${esc(p.name)}<span class="n">${p.count}</span></div>`).join("");
    $$(".chip", wrap).forEach(c => c.onclick = () => {
      S.music.playlist = c.dataset.pl; S.music.page = 1;
      $$(".chip", wrap).forEach(x => x.classList.toggle("on", x === c));
      loadMusic();
    });
  }

  async function loadMusic(append) {
    if (!S.music.playlist) { $("#musicList").innerHTML = '<div class="empty"><div class="big">🎵</div>请选择左侧歌单</div>'; return; }
    if (!append) { S.music.page = 1; $("#musicList").innerHTML = '<div class="loading"><span class="spin"></span>加载中…</div>'; }
    const p = new URLSearchParams({ playlist: S.music.playlist, page: S.music.page, limit: S.music.limit });
    if (S.music.keyword) p.set("keyword", S.music.keyword);
    const d = await api("/music/list?" + p.toString());
    const items = d.items || [];
    S.music.total = d.total || 0;
    if (!append) S.music.items = items; else S.music.items = S.music.items.concat(items);
    const box = $("#musicList");
    if (!S.music.items.length) { box.innerHTML = '<div class="empty">没有歌曲</div>'; return; }
    box.innerHTML = S.music.items.map((n, i) => `<div class="row" data-name="${esc(n)}">
      <div class="idx">${i + 1}</div><div class="nm">${esc(n)}</div>
      <div class="acts"><button class="btn sm" data-play="1">▶ 播放</button></div></div>`).join("");
    $$(".row", box).forEach(r => r.onclick = () => playMusic(r.dataset.name));
    $("#mCount").textContent = `共 ${S.music.total} 首，已显示 ${S.music.items.length} 首`;
    $("#mMore").style.display = S.music.items.length < S.music.total ? "" : "none";
  }

  async function playMusic(name) {
    if (!S.did) { toast("请先选择音箱", "err"); return; }
    const d = await api("/player/command", { method: "POST", body: {
      did: S.did, cmd: "play", name: name } });
    if (d.ret === "OK") { toast("正在播放：" + name, "ok"); setTimeout(pollPlaying, 900); }
    else toast(d.msg || "播放失败", "err");
  }

  function initMusicUI() {
    let tm = null;
    $("#mSearch").oninput = (e) => {
      clearTimeout(tm);
      tm = setTimeout(() => { S.music.keyword = e.target.value.trim(); loadMusic(); }, 320);
    };
    $("#mMore").onclick = () => { S.music.page++; loadMusic(true); };
    $("#mRefresh").onclick = async (e) => {
      e.target.disabled = true; e.target.innerHTML = '<span class="spin"></span>扫描中…';
      const d = await api("/music/refresh", { method: "POST" });
      e.target.disabled = false; e.target.textContent = "🔄 重新扫描音乐库";
      toast(d.ret === "OK" ? "扫描完成" : (d.msg || "失败"), d.ret === "OK" ? "ok" : "err");
      if (d.ret === "OK") { await loadPlaylists(); loadMusic(); }
    };
    $("#mPlayAll").onclick = () => {
      if (!S.music.items.length) { toast("列表为空"); return; }
      playMusic(S.music.items[0]);
    };
  }

  /* ---------------------------------------------------------- 网络搜歌 */
  async function doNetSearch() {
    const kw = $("#nmSearch").value.trim();
    if (!kw) { toast("请输入歌名或歌手"); return; }
    const box = $("#nmList");
    box.innerHTML = '<div class="loading"><span class="spin"></span>搜索中…</div>';
    $("#nmCount").textContent = "搜索中…";
    try {
      const d = await api("/netmusic/search?keyword=" + encodeURIComponent(kw) + "&limit=30");
      if (d.ret !== "OK") { box.innerHTML = '<div class="empty">搜索失败</div>'; $("#nmCount").textContent = "-"; toast(d.msg || "搜索失败", "err"); return; }
      S.netmusic.items = d.items || [];
      S.netmusic.searched = true;
      renderNetList();
    } catch (e) {
      box.innerHTML = '<div class="empty">搜索失败，请重试</div>';
    }
  }

  function renderNetList() {
    const box = $("#nmList");
    const items = S.netmusic.items;
    $("#nmCount").textContent = `找到 ${items.length} 首`;
    if (!items.length) {
      box.innerHTML = '<div class="empty"><div class="big">🔍</div>没有找到相关歌曲，换个关键词试试</div>';
      return;
    }
    box.innerHTML = items.map((s, i) => `<div class="row" data-i="${i}">
      <div class="idx">${i + 1}</div>
      <div class="nm"><div>${esc(s.name)}</div>
        <div class="sub">${esc(s.artist || "未知歌手")} · ${esc(s.album || "")} ${fmtTime(s.duration)}</div></div>
      <div class="acts">
        <button class="btn sm" data-dl="${i}">⬇ 下载</button>
        <button class="btn sm primary" data-dlplay="${i}">⬇▶ 下载并播放</button>
      </div></div>`).join("");
    $$("[data-dl]", box).forEach(b => b.onclick = (e) => { e.stopPropagation(); downloadSong(+b.dataset.dl, false); });
    $$("[data-dlplay]", box).forEach(b => b.onclick = (e) => { e.stopPropagation(); downloadSong(+b.dataset.dl, true); });
  }

  async function downloadSong(i, playAfter) {
    const s = S.netmusic.items[i];
    if (!s) return;
    if (playAfter && !S.did) { toast("请先选择音箱", "err"); return; }
    const btn = $$("[data-dl],[data-dlplay]", $("#nmList"))[i];
    const oldHtml = btn ? btn.innerHTML : "";
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span>'; }
    try {
      const d = await api("/netmusic/download", { method: "POST", body: {
        id: s.id, name: s.name, artist: s.artist,
        play_after: playAfter, did: S.did } });
      if (d.ret === "OK") {
        toast((d.existed ? "已存在：" : "已下载到「网络歌曲」：") + d.song, "ok");
        await loadNetLocal();
        if (playAfter && d.played) setTimeout(pollPlaying, 900);
        else if (playAfter && d.played === false) toast("已下载，但自动播放失败：" + (d.play_msg || ""), "err");
      } else {
        toast(d.msg || "下载失败", "err");
      }
    } catch (e) { toast("下载请求失败", "err"); }
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml; }
  }

  async function loadNetLocal() {
    try {
      const d = await api("/netmusic/local");
      if (d.ret !== "OK") return;
      S.netmusic.local = d.items || [];
      $("#nmLocalCount").textContent = S.netmusic.local.length;
      const box = $("#nmLocalList");
      if (!S.netmusic.local.length) {
        box.innerHTML = '<div class="empty" style="padding:18px 8px">还没有下载过歌曲，在上面搜一首试试</div>';
        return;
      }
      box.innerHTML = S.netmusic.local.map((n, i) => `<div class="row" data-name="${esc(n)}">
        <div class="idx">${i + 1}</div><div class="nm">${esc(n)}</div>
        <div class="acts"><button class="btn sm" data-play="1">▶ 播放</button></div></div>`).join("");
      $$(".row", box).forEach(r => r.onclick = () => playMusic(r.dataset.name));
    } catch (e) { /* ignore */ }
  }

  function initNetMusicUI() {
    const doIt = () => doNetSearch();
    $("#nmDoSearch").onclick = doIt;
    $("#nmSearch").onkeydown = (e) => { if (e.key === "Enter") doIt(); };
  }

  /* ---------------------------------------------------------- 设备 */
  function renderDevices() {
    const box = $("#devList");
    if (!S.devices.length) { box.innerHTML = '<div class="card empty">未发现设备，请检查小米账号配置</div>'; return; }
    box.innerHTML = S.devices.map(d => `<div class="card">
      <h3 style="margin:0 0 10px;font-size:16px">🔊 ${esc(d.name)}</h3>
      <div class="kv"><span class="k">设备 ID</span><span class="v">${esc(d.did)}</span></div>
      <div class="kv"><span class="k">型号</span><span class="v">${esc(d.hardware || "-")}</span></div>
      <div class="kv"><span class="k">状态</span><span class="v">${d.is_playing
        ? '<span class="tag ok">播放中</span>' : '<span class="tag">空闲</span>'}</span></div>
      <div class="kv"><span class="k">当前曲目</span><span class="v">${esc(d.cur_music || "-")}</span></div>
      <div class="kv"><span class="k">当前歌单</span><span class="v">${esc(d.cur_playlist || "-")}</span></div>
      <div class="toolbar" style="margin-top:12px">
        <button class="btn sm" data-dstop="${esc(d.did)}">⏹ 停止</button>
        <button class="btn sm" data-duse="${esc(d.did)}">✓ 设为当前</button>
      </div></div>`).join("");
    $$("[data-dstop]", box).forEach(b => b.onclick = async () => {
      await api("/player/command", { method: "POST", body: { did: b.dataset.dstop, cmd: "stop" } });
      toast("已发送停止指令");
    });
    $$("[data-duse]", box).forEach(b => b.onclick = () => {
      S.did = b.dataset.duse; $("#devSel").value = S.did; toast("已切换设备", "ok"); pollPlaying();
    });
  }

  /* ---------------------------------------------------------- 系统 */
  async function loadSysInfo() {
    const d = await api("/system/info");
    if (d.ret !== "OK") { $("#sysInfo").innerHTML = "读取失败"; return; }
    const disk = d.disk ? `${fmtSize(d.disk.used)} / ${fmtSize(d.disk.total)}（剩余 ${fmtSize(d.disk.free)}）` : "-";
    $("#sysInfo").innerHTML = `
      <div class="kv"><span class="k">程序版本</span><span class="v">${esc(d.app.name)} v${esc(d.app.version)}</span></div>
      <div class="kv"><span class="k">服务地址</span><span class="v">${esc(d.hostname)}:${esc(d.port)}</span></div>
      <div class="kv"><span class="k">音乐目录</span><span class="v">${esc(d.music_path)}</span></div>
      <div class="kv"><span class="k">磁盘占用</span><span class="v">${disk}</span></div>
      <div class="kv"><span class="k">运行时长</span><span class="v">${fmtUp(d.uptime)}</span></div>
      <div class="kv"><span class="k">服务器时间</span><span class="v">${esc(d.server_time)}</span></div>
      <div class="kv"><span class="k">Python</span><span class="v">${esc(d.python)}</span></div>`;
  }

  async function loadConfig() {
    const d = await api("/config");
    if (d.ret !== "OK") return;
    const c = d.config || {};
    $("#cfgAccount").value = c.account || "";
    $("#cfgHostname").value = c.hostname || "";
    $("#cfgPassword").value = "";
  }

  function initSystemUI() {
    $("#cfgReload").onclick = loadConfig;
    $("#cfgSave").onclick = async () => {
      const body = { account: $("#cfgAccount").value, hostname: $("#cfgHostname").value };
      const pw = $("#cfgPassword").value;
      if (pw) body.password = pw;
      const d = await api("/config", { method: "POST", body: body });
      if (d.ret !== "OK") { toast(d.msg || "保存失败", "err"); return; }
      toast("已保存，正在重新登录…", "ok");
      const r = await api("/system/reinit", { method: "POST" });
      toast(r.ret === "OK" ? "重新初始化完成" : (r.msg || "初始化失败"), r.ret === "OK" ? "ok" : "err");
      setTimeout(loadStatus, 1500);
    };
    $("#logLoad").onclick = async () => {
      $("#logBox").textContent = "加载中…";
      const d = await api("/system/logs?lines=200");
      $("#logBox").textContent = d.ret === "OK" ? (d.lines || []).join("\n") : ("读取失败: " + d.msg);
      $("#logBox").scrollTop = $("#logBox").scrollHeight;
    };
  }

  /* ---------------------------------------------------------- 睡眠模式 */
  async function loadSleep() {
    if (!S.did) return;
    try {
      const d = await api("/sleep/status?did=" + encodeURIComponent(S.did));
      const tip = $("#sleepTip");
      if (d.active && d.timer) {
        S.sleep = d.timer;
        S._sleepDone = false;
        $("#pSleep").style.color = "var(--brand)";
        $("#pSleep").title = "睡眠模式进行中，剩余 " + d.timer.remain_text;
        if (tip) tip.innerHTML = `🌙 睡眠模式进行中：剩余 <b>${d.timer.remain_text}</b>，` +
          `当前音量 <b>${d.timer.current_volume}</b>（起始 ${d.timer.start_volume}）`;
      } else {
        const wasActive = !!S.sleep;
        S.sleep = null;
        $("#pSleep").style.color = "";
        $("#pSleep").title = "睡眠模式（渐弱定时关闭）";
        if (tip) tip.textContent = "开启后音量会从当前音量慢慢减小，到点自动静音并停止播放。";
        // 定时器刚跑完：提示一次（只提示一次，避免每 5 秒刷屏）
        if (d.finished && !S._sleepDone) {
          S._sleepDone = true;
          toast("🌙 睡眠模式已结束，已静音并停止播放", "ok");
        } else if (wasActive && !d.finished) {
          S._sleepDone = false;
        }
      }
    } catch (e) { /* ignore */ }
  }

  async function sleepStart(minutes) {
    if (!S.did) { toast("请先选择音箱", "err"); return; }
    const d = await api("/sleep/start", { method: "POST", body: { did: S.did, minutes: minutes } });
    if (d.ret === "OK") {
      toast(`🌙 已开启睡眠模式：这个功能是 ${minutes} 分钟静音，音量将慢慢减小`, "ok");
      loadSleep();
    } else toast(d.msg || "开启失败", "err");
  }

  async function sleepCancel() {
    if (!S.did) return;
    const d = await api("/sleep/cancel", { method: "POST", body: { did: S.did } });
    if (d.ret === "OK") { toast("已取消睡眠模式", "ok"); loadSleep(); }
    else toast("当前没有运行中的睡眠模式");
  }

  function showSleepDialog() {
    openModal(`<h3>🌙 睡眠模式</h3>
      <p style="color:var(--text-2);font-size:13px;margin-top:-6px">
        这个功能是 <b>45 分钟静音</b>（可改）：音量会从当前音量慢慢减小，到点自动静音并停止播放。</p>
      <div class="chips" style="margin:14px 0">
        ${[15, 30, 45, 60, 90, 120].map(m =>
          `<div class="chip ${m === 45 ? "on" : ""}" data-min="${m}">${m} 分钟</div>`).join("")}
      </div>
      <div class="foot">
        <button class="btn" id="sdCancel">取消定时</button>
        <button class="btn primary" id="sdOk">开始渐弱</button>
      </div>`);
    let pick = 45;
    $$("[data-min]", $("#modal")).forEach(c => c.onclick = () => {
      pick = +c.dataset.min;
      $$("[data-min]", $("#modal")).forEach(x => x.classList.toggle("on", x === c));
    });
    $("#sdCancel").onclick = async () => { await sleepCancel(); closeModal(); };
    $("#sdOk").onclick = async () => { closeModal(); await sleepStart(pick); };
  }

  function initSleepUI() {
    const s = $("#sleepStart"), c = $("#sleepCancel");
    if (s) s.onclick = () => sleepStart(+$("#sleepMin").value);
    if (c) c.onclick = sleepCancel;
    $("#pSleep").onclick = showSleepDialog;
  }

  /* ---------------------------------------------------------- 启动 */
  async function boot() {
    initTheme(); initNav(); initPlayerBar(); initRadioUI(); initMusicUI();
    initSystemUI(); initSleepUI(); initNormalizeUI(); initNetMusicUI();
    $("#devSel").onchange = (e) => { S.did = e.target.value; pollPlaying(); };
    $("#mask").onclick = (e) => { if (e.target.id === "mask") closeModal(); };

    await loadStatus();
    await loadTree();
    await loadRadio();
    loadRecent();
    loadNormalize();

    setInterval(pollPlaying, 1000);
    setInterval(() => { if ($("#page-dash").classList.contains("on")) loadRecent(); }, 8000);
    setInterval(loadStatus, 20000);
    pollPlaying();
  }

  async function loadRecent() {
    const d = await api("/radio/recent?limit=12");
    const box = $("#recentList");
    if (d.ret !== "OK" || !(d.items || []).length) {
      box.innerHTML = '<div class="empty" style="padding:26px 8px"><div class="big">📻</div>还没有播放记录</div>';
      return;
    }
    box.innerHTML = d.items.map((s, i) => `<div class="row" data-id="${esc(s.id)}">
      <div class="idx">${i + 1}</div><div class="nm">${esc(s.name)}</div>
      <div class="acts"><button class="btn sm">▶</button></div></div>`).join("");
    $$(".row", box).forEach(r => r.onclick = () => playRadio(r.dataset.id));
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
