/* ADCMT 7461A 上位机前端 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const state = {
    meta: null,
    device: {},
    instrument: {},
    config: { display_mode: 'voltage', shunt_ohm: 0.1 },
    limits: { voltage: { enabled: false, low: null, high: null }, current: { enabled: false, low: null, high: null } },
    alarm: 'off',
    logging: {},
    stats: {},
    contextId: 0,
    samples: [],
    latest: null,
    windowSec: 60,
    view: { live: true, start: 0, end: 0 },
    theme: document.documentElement.dataset.theme || 'dark',
    tableOpen: false,
    readoutDirty: false,
    switching: false,
    switchSeq: 0,
    serverOnline: false,
    shuntDirty: false,
    limitsDirty: false,
    limitsMode: null,
    lastLogsFetch: 0,
    statMetrics: null,
    displayDigits: 7,
    null: { enabled: false, const: null },
    hold: null,            // 保持（HOLD）时冻结的样本
    lastSampleAt: 0,       // 最近一次收到样本的本地时间 (ms)
    sim: false,
    knownLogs: null,
  };

  const STAT_DEFS = [
    { key: 'min', label: '最小值', fmt: 'si' },
    { key: 'max', label: '最大值', fmt: 'si' },
    { key: 'avg', label: '平均值', fmt: 'si' },
    { key: 'median', label: '中位数', fmt: 'si' },
    { key: 'rms', label: '均方根 RMS', fmt: 'si' },
    { key: 'pp', label: '峰峰值', fmt: 'si' },
    { key: 'std', label: '标准差（样本）', fmt: 'si' },
    { key: 'std_pop', label: '标准差（总体）', fmt: 'si' },
    { key: 'var_sample', label: '方差（样本）', fmt: 'si2' },
    { key: 'mad', label: '中位绝对偏差', fmt: 'si' },
    { key: 'mean_abs_dev', label: '平均绝对偏差', fmt: 'si' },
    { key: 'crest', label: '波峰因数', fmt: 'plain' },
    { key: 'slope', label: '趋势斜率', fmt: 'slope' },
    { key: 'sum', label: '总和', fmt: 'si' },
    { key: 'first', label: '首值', fmt: 'si' },
    { key: 'last', label: '末值', fmt: 'si' },
    { key: 'min_t', label: '最小值时间', fmt: 'time' },
    { key: 'max_t', label: '最大值时间', fmt: 'time' },
    { key: 'n', label: '有效样本', fmt: 'int' },
    { key: 'n_total', label: '总样本', fmt: 'int' },
    { key: 'n_ol', label: 'OL 次数', fmt: 'int' },
    { key: 'ol_ratio', label: 'OL 比例', fmt: 'percent' },
    { key: 'rate_hz', label: '采样率', fmt: 'rate' },
    { key: 'duration', label: '统计时长', fmt: 'duration' },
  ];
  const DEFAULT_STAT_METRICS = [
    'min', 'max', 'avg', 'pp', 'std', 'rms',
    'median', 'slope', 'n', 'n_total', 'n_ol', 'ol_ratio',
  ];

  const fmtCache = {};
  let itLastDeviceState = null;
  let itLoading = false;

  // ------------------------------------------------------------------
  // 工具
  // ------------------------------------------------------------------
  const PREFIXES = [
    [1e9, 'G'], [1e6, 'M'], [1e3, 'k'], [1, ''],
    [1e-3, 'm'], [1e-6, 'µ'], [1e-9, 'n'], [1e-12, 'p'],
  ];

  // 工程计数法：按有效位数固定小数位，保留末尾的 0（如 12.50000 mV）
  function fmtEng(value, baseUnit, sig = 6) {
    if (value === null || value === undefined || !Number.isFinite(value)) {
      return { text: 'OL', unit: baseUnit };
    }
    let idx = PREFIXES.findIndex(([f]) => f === 1);
    const abs = Math.abs(value);
    if (value !== 0) {
      idx = PREFIXES.findIndex(([f]) => abs >= f);
      if (idx < 0) idx = PREFIXES.length - 1;     // < 1 p
    }
    const decimals = (a) => Math.min(9, Math.max(0,
      sig - (a >= 100 ? 3 : a >= 10 ? 2 : 1)));
    let scaled = value / PREFIXES[idx][0];
    let digits = decimals(Math.abs(scaled));
    let text = scaled.toFixed(digits);
    // 四舍五入后可能进位（9.9999996 → 10.000000，999.99995 m → 1000.0000 m），重新定位数/词头
    const r = Math.abs(Number(text));
    if (r >= 1000 && idx > 0) {
      idx -= 1;
      scaled = value / PREFIXES[idx][0];
      digits = decimals(Math.abs(Number(scaled.toFixed(digits + 3))));
      text = scaled.toFixed(digits);
    } else if (decimals(r) !== digits) {
      digits = decimals(r);
      text = scaled.toFixed(digits);
    }
    if (Number(text) === 0 && text.startsWith('-')) text = text.slice(1);
    return { text, unit: PREFIXES[idx][1] + (baseUnit || '') };
  }

  function fmtR(ohm) {
    if (!Number.isFinite(ohm)) return '—';
    const v = fmtEng(ohm, 'Ω', 4);
    return v.text + ' ' + v.unit;
  }

  function timeStr(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString('zh-CN', { hour12: false }) + '.' +
      String(d.getMilliseconds()).padStart(3, '0');
  }

  function funcLabel(code) {
    const f = state.meta && state.meta.functions.find(x => x.code === code);
    return f ? f.label : (code || '—');
  }

  function fnMeta(code) {
    return state.meta && state.meta.functions.find(x => x.code === code);
  }

  // 功能配置（后端 FUNCTION_PROFILES）：可用量程与功能专属设置
  function fnFeatures(code) {
    const f = fnMeta(code || state.instrument.function || 'DCV');
    return (f && f.features) || [];
  }
  function hasFeature(name, code) { return fnFeatures(code).includes(name); }
  function fixedRange(code) {
    const f = fnMeta(code || state.instrument.function || 'DCV');
    return f && f.fixed_range ? f.fixed_range : null;
  }

  const UNIT_NAMES = { V: '电压', A: '电流', 'Ω': '电阻', Hz: '频率' };

  function functionBaseUnit() {
    const f = fnMeta(state.instrument.function);
    return f ? f.unit : 'V';
  }

  function baseUnit() {
    if (state.config.display_mode === 'current') return 'A';
    return functionBaseUnit();
  }

  function displayUnit() {
    return baseUnit();
  }

  function limitKey() {
    const u = displayUnit();
    if (u === 'A') return 'current';
    if (u === 'V') return 'voltage';
    return null;
  }

  const FN_GROUPS = [
    { label: '电压', codes: ['DCV', 'ACV', 'ACV_DC', 'DCV_BCH'] },
    { label: '电阻', codes: ['RES2W', 'RES4W', 'RES2W_LP', 'RES4W_LP', 'CONT'] },
    { label: '电流', codes: ['DCI', 'ACI', 'ACI_DC'] },
    { label: '其他', codes: ['DIODE', 'FREQ'] },
  ];

  function setText(id, text) {
    const el = $(id);
    if (el && el.textContent !== text) el.textContent = text;
  }

  // 堆叠式提示：相同消息合并计数，错误停留更久，点击可关闭
  function showToast(msg, kind = '') {
    const stack = $('toastStack');
    if (!stack) return;
    const existing = [...stack.children].find(t =>
      t.dataset.msg === msg && t.dataset.kind === kind && !t.classList.contains('leaving'));
    let el = existing;
    if (el) {
      const n = (parseInt(el.dataset.count, 10) || 1) + 1;
      el.dataset.count = String(n);
      let c = el.querySelector('.count');
      if (!c) { c = document.createElement('span'); c.className = 'count'; el.appendChild(c); }
      c.textContent = '×' + n;
    } else {
      el = document.createElement('div');
      el.className = 'toast ' + kind;
      el.dataset.msg = msg;
      el.dataset.kind = kind;
      el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
      const span = document.createElement('span');
      span.textContent = msg;
      el.appendChild(span);
      el.addEventListener('click', () => dismissToast(el));
      stack.appendChild(el);
      while (stack.children.length > 4) dismissToast(stack.firstElementChild, true);
    }
    clearTimeout(el._timer);
    el._timer = setTimeout(() => dismissToast(el), kind === 'error' ? 5200 : 2600);
  }

  function dismissToast(el, immediate = false) {
    if (!el || !el.parentNode) return;
    clearTimeout(el._timer);
    if (immediate || REDUCED_MOTION) { el.remove(); return; }
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 220);
  }

  const REDUCED_MOTION = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // 按钮请求中状态：防重复点击并显示旋转指示
  async function withBusy(btn, fn) {
    if (!btn || btn.classList.contains('is-busy')) return undefined;
    const wasDisabled = btn.disabled;
    btn.classList.add('is-busy');
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    try {
      return await fn();
    } finally {
      btn.classList.remove('is-busy');
      btn.removeAttribute('aria-busy');
      btn.disabled = wasDisabled;
    }
  }

  function restartClass(el, cls) {
    if (!el) return;
    el.classList.remove(cls);
    void el.offsetWidth;   // 触发重排以重新播放动画
    el.classList.add(cls);
  }

  function flashOk(...els) {
    for (const el of els) {
      if (!el) continue;
      restartClass(el, 'flash-ok');
      setTimeout(() => el.classList.remove('flash-ok'), 950);
    }
  }

  function markInvalid(el, msg) {
    if (el) {
      restartClass(el, 'invalid');
      el.focus();
      const clear = () => { el.classList.remove('invalid'); el.removeEventListener('input', clear); };
      el.addEventListener('input', clear);
      setTimeout(clear, 2400);
    }
    if (msg) showToast(msg, 'error');
  }

  function onEnter(el, fn) {
    if (!el) return;
    el.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.isComposing) {
        ev.preventDefault();
        fn();
      }
    });
  }

  function isOnline() {
    return (state.device.state || 'offline') === 'online';
  }
  // 有数据流：在线或已释放给面板（仍被动接收数据）
  function hasData() {
    const st = state.device.state || 'offline';
    return st === 'online' || st === 'local';
  }
  function isReleased() {
    return !!state.released || (state.device.state || '') === 'local';
  }

  // 自定义确认框，替代 window.confirm
  function confirmDialog(text, { title = '确认操作', ok = '确认执行' } = {}) {
    return new Promise((resolve) => {
      const layer = $('confirmLayer');
      setText('confirmTitle', title);
      setText('confirmText', text);
      setText('confirmOk', ok);
      layer.classList.remove('hidden');
      const prevFocus = document.activeElement;
      $('confirmCancel').focus();
      const done = (val) => {
        layer.classList.add('hidden');
        $('confirmOk').removeEventListener('click', onOk);
        $('confirmCancel').removeEventListener('click', onCancel);
        layer.removeEventListener('click', onBackdrop);
        document.removeEventListener('keydown', onKey, true);
        if (prevFocus && prevFocus.focus) prevFocus.focus();
        resolve(val);
      };
      const onOk = () => done(true);
      const onCancel = () => done(false);
      const onBackdrop = (e) => { if (e.target === layer) done(false); };
      const onKey = (e) => {
        if (e.key === 'Escape') { e.stopPropagation(); e.preventDefault(); done(false); }
      };
      $('confirmOk').addEventListener('click', onOk);
      $('confirmCancel').addEventListener('click', onCancel);
      layer.addEventListener('click', onBackdrop);
      document.addEventListener('keydown', onKey, true);
    });
  }

  function fmtClock(sec) {
    sec = Math.max(0, Math.floor(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    const p = (n) => String(n).padStart(2, '0');
    return `${p(h)}:${p(m)}:${p(s)}`;
  }

  function fmtAgo(ts) {
    const d = Date.now() / 1000 - ts;
    if (d < 60) return '刚刚';
    if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
    if (d < 86400) return Math.floor(d / 3600) + ' 小时前';
    return new Date(ts * 1000).toLocaleDateString('zh-CN');
  }

  async function api(path, opts = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    try {
      const res = await fetch(path, { signal: ctrl.signal, ...opts });
      let data = null;
      try { data = await res.json(); } catch (_) { /* ignore */ }
      if (!res.ok || (data && data.ok === false)) {
        throw new Error((data && data.error) || `请求失败 (HTTP ${res.status})`);
      }
      return data;
    } catch (err) {
      if (err.name === 'AbortError') throw new Error('请求超时');
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  function postJSON(path, body) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  }

  // ------------------------------------------------------------------
  // 主题
  // ------------------------------------------------------------------
  function applyTheme(theme) {
    state.theme = theme;
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('dmm-theme', theme); } catch (_) {}
    $('iconMoon').classList.toggle('hidden', theme === 'dark');
    $('iconSun').classList.toggle('hidden', theme !== 'dark');
    chart.dirty = true;
  }

  $('themeToggle').addEventListener('click', () => {
    applyTheme(state.theme === 'dark' ? 'light' : 'dark');
  });

  // ------------------------------------------------------------------
  // 渲染：设备状态 / 读数
  // ------------------------------------------------------------------
  function renderDevice() {
    const dev = state.device || {};
    let st = dev.state || 'offline';
    let text = dev.message || (st === 'online' ? '已连接' : '未连接');
    if (!state.serverOnline) { st = 'offline'; text = '服务器未连接'; }
    const led = $('statusLed');
    led.className = 'led ' + (st === 'online' ? 'online'
      : st === 'connecting' ? 'connecting'
      : st === 'local' ? 'local' : 'offline');
    setText('statusText', text);
    $('connPill').classList.toggle('is-offline', st === 'offline');
    $('connPill').title = text;
    setText('deviceId', dev.idn || '—');
    setText('serverInfo', dev.serial
      ? `设备 ${dev.serial} · ${state.sim ? '模拟器' : '真机'}`
      : (state.sim ? '模拟器' : ''));

    const offline = st !== 'online';
    $('readout').classList.toggle('offline', !hasData() || !state.serverOnline);
    renderRelease();
    for (const id of ['rangeSelect', 'rateSelect', 'triggerSelect']) {
      const el = $(id);
      if (el && !el.classList.contains('pending')) el.disabled = offline || state.switching;
    }
    if (fixedRange()) $('rangeSelect').disabled = true;
    for (const id of ['itValue', 'itUnit', 'itApply']) {
      const el = $(id);
      if (el && !el.classList.contains('is-busy')) el.disabled = offline;
    }
    for (const b of $('fnList').querySelectorAll('.fn-item')) b.disabled = offline;
    const fixed = fixedRange();
    $('controlHint').textContent = offline
      ? '设备离线，设置不可用。'
      : (state.config.display_mode === 'current'
          ? '分流电流模式使用 DCV 档；点击其它功能会自动退出分流模式。'
          : (fixed ? `${funcLabel(state.instrument.function)}量程固定为 ${fixed}。`
                   : '选择后立即生效，以仪器回读值为准。'));
    if (st === 'online' && itLastDeviceState !== 'online') {
      itLastDeviceState = 'online';
      loadIntegrationTime();
    } else if (st !== 'online') {
      itLastDeviceState = st;
      const itEl = $('itReadback');
      if (itEl) itEl.textContent = '当前：—';
    }
    renderNull();
    setZoomUI();
  }

  function renderNull() {
    const info = (state.null && typeof state.null === 'object')
      ? state.null : { enabled: false, const: null };
    const online = (state.device.state || 'offline') === 'online';
    const toggle = $('nullToggle');
    const take = $('nullTake');
    if (toggle) {
      toggle.checked = !!info.enabled;
      toggle.disabled = !online;
    }
    if (take && !take.classList.contains('is-busy')) take.disabled = !online;
    setText('nullStateText', info.enabled ? '开启' : '关闭');
    const badge = $('nullBadge');
    if (badge) badge.classList.toggle('on', !!info.enabled);
    const ref = $('nullRef');
    if (!ref) return;
    if (info.const === null || info.const === undefined) {
      ref.textContent = '—';
      return;
    }
    let value = Number(info.const);
    let unit;
    if (state.config.display_mode === 'current') {
      const r = Number(state.config.shunt_ohm) || 1;
      value = value / r;
      unit = 'A';
    } else {
      unit = baseUnit();
    }
    const x = fmtEng(value, unit, state.displayDigits || 7);
    ref.textContent = x.text + ' ' + x.unit;
  }

  async function loadIntegrationTime() {
    const el = $('itReadback');
    if (!el) return;
    if ((state.device.state || 'offline') !== 'online') {
      el.textContent = '当前：—';
      return;
    }
    if (itLoading) return;
    itLoading = true;
    try {
      const d = await api('/api/advanced/read?keys=itp,its,digits');
      const itp = d.values && d.values.itp;
      const its = d.values && d.values.its;
      const digits = d.values && d.values.digits;
      if (digits && digits.ok) {
        const m = String(digits.value).match(/RE(\d)/i);
        if (m) {
          const n = parseInt(m[1], 10);
          state.displayDigits = ({ 3: 4, 4: 5, 5: 6, 6: 7 })[n] || 7;
        }
      }
      const parts = [];
      if (itp && itp.ok) {
        const n = parseAdvNumber(itp.value);
        if (n !== null) parts.push(trimNum(n) + ' PLC');
      }
      if (its && its.ok) {
        const n = parseAdvNumber(its.value);
        if (n !== null) parts.push(trimNum(n) + ' s');
      }
      el.textContent = parts.length ? '当前：' + parts.join(' / ') : '当前：—';
    } catch (_) {
      el.textContent = '当前：—';
    } finally {
      itLoading = false;
    }
  }

  async function applyIntegrationTime() {
    if ((state.device.state || 'offline') !== 'online') {
      showToast('设备未连接，无法设置积分时间', 'error');
      return;
    }
    const input = $('itValue');
    const value = parseFloat(input.value);
    if (!Number.isFinite(value)) {
      markInvalid(input, '请输入有效的积分时间');
      return;
    }
    const unit = $('itUnit').value;
    const key = unit === 'plc' ? 'itp' : 'its';
    const min = unit === 'plc' ? 0.02 : 0.00001;
    const max = unit === 'plc' ? 100 : 10;
    if (value < min || value > max) {
      markInvalid(input, `范围 ${min} – ${max} ${unit === 'plc' ? 'PLC' : 's'}`);
      return;
    }
    await withBusy($('itApply'), async () => {
      try {
        await postJSON('/api/advanced/set', { key, value });
        showToast('积分时间已设置，已切换为自由采样', 'ok');
        flashOk(input);
        await loadIntegrationTime();
        if (advIsOpen()) loadAdvKeys(['itp', 'its']);
      } catch (err) {
        showToast(err.message, 'error');
      }
    });
  }

  $('itApply').addEventListener('click', applyIntegrationTime);
  onEnter($('itValue'), applyIntegrationTime);
  $('itUnit').addEventListener('change', () => {
    $('itValue').placeholder = $('itUnit').value === 'plc' ? '如 1.5' : '如 0.02';
  });

  $('nullToggle').addEventListener('change', async () => {
    if ((state.device.state || 'offline') !== 'online') {
      renderNull();
      showToast('设备未连接，无法设置 NULL', 'error');
      return;
    }
    const enable = $('nullToggle').checked;
    $('nullToggle').disabled = true;
    if ($('nullTake')) $('nullTake').disabled = true;
    try {
      if (enable) {
        const d = await postJSON('/api/advanced/action', { key: 'null_const' });
        state.null = {
          enabled: true,
          const: (d.reference !== undefined && d.reference !== null)
            ? d.reference : (state.null && state.null.const),
        };
        showToast(d.warning ? 'NULL 已启用（回读稍后同步）'
          : 'NULL 已启用，当前读数已设为参考', 'ok');
      } else {
        const d = await postJSON('/api/advanced/set', { key: 'null', value: 'NL0' });
        state.null = { enabled: false, const: state.null && state.null.const };
        showToast(d.warning ? 'NULL 已关闭（回读稍后同步）' : 'NULL 已关闭', 'ok');
      }
    } catch (err) {
      showToast(err.message, 'error');
    } finally {
      renderNull();
      renderDevice();
    }
  });

  $('nullTake').addEventListener('click', async () => {
    if ((state.device.state || 'offline') !== 'online') {
      showToast('设备未连接，无法设置 NULL', 'error');
      return;
    }
    const btn = $('nullTake');
    if ($('nullToggle')) $('nullToggle').disabled = true;
    await withBusy(btn, async () => {
      try {
        const d = await postJSON('/api/advanced/action', { key: 'null_const' });
        state.null = {
          enabled: true,
          const: (d.reference !== undefined && d.reference !== null)
            ? d.reference : (state.null && state.null.const),
        };
        showToast(d.warning ? '已重新取参考（回读稍后同步）'
          : '已把当前读数设为 NULL 参考', 'ok');
        flashOk($('nullRef'));
      } catch (err) {
        showToast(err.message, 'error');
      }
    });
    renderNull();
    renderDevice();
  });

  function readoutText(s) {
    if (!s) return null;
    if (s.ol || s.d === null || s.d === undefined) return 'OL';
    const v = fmtEng(s.d, s.u || baseUnit(), state.displayDigits || 7);
    return v.text + ' ' + v.unit;
  }

  function renderReadout(sample) {
    const s = state.hold || sample || state.latest;
    const unitBase = baseUnit();
    const readout = $('readout');
    const alarm = state.hold ? (state.hold.alarm || 'off') : ((s && s.alarm) || state.alarm || 'off');
    const alarming = alarm === 'low' || alarm === 'high' || alarm === 'ol';
    readout.classList.toggle('alarm', alarming && !state.hold);
    readout.classList.toggle('hold', !!state.hold);
    readout.classList.toggle('switching', !!state.switching);
    $('alarmAnn').classList.toggle('on', alarming);
    $('holdBadge').classList.toggle('on', !!state.hold);
    document.body.classList.toggle('mode-current', state.config.display_mode === 'current');

    const code = state.instrument.function || (s && s.fn) || 'DCV';
    const meta = fnMeta(code);
    const title = meta ? meta.label : (state.instrument.function_label || '—');
    const h1 = $('pageTitle');
    if (h1.textContent !== title) {
      h1.textContent = title;
      restartClass(h1, 'swap');
    }
    setText('pageCode', code);
    setText('pageNote', meta ? (meta.note || '') : '');

    const uName = UNIT_NAMES[unitBase] || '读数';
    const badge = $('modeBadge');
    badge.textContent = state.config.display_mode === 'current' ? '分流电流' : uName;
    badge.className = 'badge ' + (unitBase === 'A' ? 'badge-current' : 'badge-voltage');

    const range = state.instrument.range;
    setText('rangeLabel', range === 'AUTO' ? 'AUTO'
      : (state.instrument.range_label ? 'RNG ' + state.instrument.range_label : '—'));
    setText('rateLabel', state.instrument.rate_label || '—');
    setText('triggerLabel', state.instrument.trigger_label
      ? 'TRIG ' + state.instrument.trigger_label : '—');
    setText('shuntInfo', fmtR(state.config.shunt_ohm));

    // 显示屏状态行
    let stateText = '';
    readout.classList.remove('stale');
    if (state.serverOnline && isReleased() && hasData() && !state.hold) {
      stateText = '面板控制中 · 只接收数据'
        + (s && s.t ? ' · ' + timeStr(s.t) : '');
    } else if (!isOnline() && !(hasData() && state.serverOnline)) {
      stateText = state.serverOnline ? '设备未连接 · 等待重连' : '服务器未连接';
    } else if (state.switching) {
      stateText = '切换中…';
    } else if (state.hold) {
      stateText = 'HOLD · ' + timeStr(state.hold.t) + ' · 再按 H 释放';
    } else if (state.lastSampleAt && Date.now() - state.lastSampleAt > staleLimitMs()) {
      readout.classList.add('stale');
      stateText = '无新数据 · ' + Math.round((Date.now() - state.lastSampleAt) / 1000) + ' s'
        + (state.instrument.trigger && state.instrument.trigger !== 'TRS0' ? ' · 等待触发' : '');
    } else if (s && s.t) {
      stateText = timeStr(s.t);
    }
    setText('displayState', stateText);

    // 通断判定：电阻低于阈值 → 导通
    const judge = $('contJudge');
    if (code === 'CONT') {
      judge.classList.remove('hidden');
      const thr = state.contThreshold;
      let pass = null;
      if (s && !state.switching) {
        pass = !s.ol && s.v !== null && s.v !== undefined && thr ? s.v < thr : (s.ol ? false : null);
      }
      judge.classList.toggle('pass', pass === true);
      judge.classList.toggle('open', pass === false);
      setText('contJudgeText', pass === true ? '导通' : pass === false ? '断开' : '—');
      setText('contJudgeSub', thr ? `阈值 < ${thr} Ω` : '阈值读取中');
    } else {
      judge.classList.add('hidden');
    }

    const showBurden = state.config.display_mode === 'current';
    $('burdenItem').classList.toggle('hidden', !showBurden);
    $('shuntItem').classList.toggle('hidden', !showBurden);

    if (!s) {
      setText('valueText', '--');
      setText('unitText', unitBase);
      setText('burdenText', '—');
      setAlarmBanner(alarm);
      return;
    }

    if (s.ol || s.d === null || s.d === undefined) {
      setText('valueText', 'OL');
      setText('unitText', s.u || unitBase);
    } else {
      const v = fmtEng(s.d, s.u || unitBase, state.displayDigits || 7);
      setText('valueText', v.text);
      setText('unitText', v.unit);
    }
    if (s.v === null || s.v === undefined) {
      setText('burdenText', 'OL');
    } else {
      const b = fmtEng(s.v, 'V', 5);
      setText('burdenText', b.text + ' ' + b.unit);
    }
    setAlarmBanner(alarm);
  }

  function staleLimitMs() {
    const rate = Number((state.stats || {}).rate_hz) || 0;
    return Math.max(3000, rate > 0 ? 5000 / rate : 0);
  }

  function setAlarmBanner(alarm) {
    const el = $('alarmBanner');
    if (alarm === 'ol') { el.textContent = '超量程 OL'; el.className = 'alarm-banner'; el.classList.remove('hidden'); }
    else if (alarm === 'low') { el.textContent = '低于下限'; el.className = 'alarm-banner'; el.classList.remove('hidden'); }
    else if (alarm === 'high') { el.textContent = '高于上限'; el.className = 'alarm-banner warn'; el.classList.remove('hidden'); }
    else { el.classList.add('hidden'); }
    updateLimitTag(alarm);
  }

  function updateLimitTag(alarm) {
    const key = limitKey();
    const tag = $('limitState');
    if (!key) {
      tag.textContent = '不支持';
      tag.className = 'tag';
      return;
    }
    const lim = (state.limits || {})[key] || {};
    let text = '未启用', cls = 'tag';
    if (lim.enabled) {
      if (alarm === 'ol') { text = '超量程'; cls = 'tag alarm'; }
      else if (alarm === 'low') { text = '低于下限'; cls = 'tag alarm'; }
      else if (alarm === 'high') { text = '高于上限'; cls = 'tag alarm'; }
      else { text = '正常'; cls = 'tag ok'; }
    }
    tag.textContent = text;
    tag.className = cls;
  }

  function formatStat(def, st, unit) {
    const v = st[def.key];
    if (v === null || v === undefined) return '—';
    if (typeof v === 'number' && !Number.isFinite(v)) return '—';
    switch (def.fmt) {
      case 'si': {
        const x = fmtEng(v, unit, 6);
        return x.text + ' ' + x.unit;
      }
      case 'si2': {
        const x = fmtEng(v, unit ? unit + '²' : '', 5);
        return x.text + ' ' + x.unit;
      }
      case 'plain': return Number(v).toFixed(3);
      case 'percent': return (Number(v) * 100).toFixed(2) + ' %';
      case 'time': return timeStr(Number(v));
      case 'int': return String(Math.round(Number(v)));
      case 'rate': return Number(v).toFixed(1) + ' 样本/秒';
      case 'duration': return Number(v).toFixed(2) + ' s';
      case 'slope': {
        const x = fmtEng(v, (unit || '') + '/s', 5);
        return x.text + ' ' + x.unit;
      }
      default: return String(v);
    }
  }

  function renderStats() {
    const st = state.stats || {};
    const unit = st.unit || baseUnit();
    const metrics = state.statMetrics || DEFAULT_STAT_METRICS;
    const grid = $('statsGrid');
    if (grid) {
      if (!metrics.length) {
        grid.innerHTML = '<div class="stats-empty">未选择统计项，点击「选择统计项」添加</div>';
      } else if (grid.dataset.keys === metrics.join(',')) {
        // 统计项未变：只更新数值，避免整块重建引起闪烁
        for (const b of grid.querySelectorAll('b[data-key]')) {
          const def = STAT_DEFS.find(d => d.key === b.dataset.key);
          const txt = def ? formatStat(def, st, unit) : '—';
          if (b.textContent !== txt) { b.textContent = txt; b.title = txt; }
        }
      } else {
        grid.innerHTML = metrics.map(key => {
          const def = STAT_DEFS.find(d => d.key === key);
          if (!def) return '';
          const txt = formatStat(def, st, unit);
          return `<div class="stat"><label>${def.label}</label><b data-key="${key}" title="${txt}">${txt}</b></div>`;
        }).join('');
      }
      grid.dataset.keys = metrics.length ? metrics.join(',') : '';
    }
    setText('sampleCount', String(st.n_total || 0));
    const rate = st.rate_hz;
    const online = isOnline();
    setText('sampleRate', rate ? `${rate >= 10 ? Math.round(rate) : Number(rate).toFixed(1)} S/s`
      : (online ? '—' : ''));

    // 副显示：MIN / AVG / MAX / P-P
    const sub = (key, id) => {
      const def = STAT_DEFS.find(d => d.key === key);
      setText(id, def ? formatStat(def, st, unit) : '—');
    };
    sub('min', 'subMin');
    sub('avg', 'subAvg');
    sub('max', 'subMax');
    sub('pp', 'subPp');
    setText('statsSince', st.since && st.n_total
      ? `自 ${new Date(st.since * 1000).toLocaleTimeString('zh-CN', { hour12: false })} 起`
      : '');
    if ($('statsConfigCount')) {
      $('statsConfigCount').textContent = `${metrics.length} / ${STAT_DEFS.length} 项`;
    }
  }

  function loadStatMetrics() {
    try {
      const raw = JSON.parse(localStorage.getItem('dmm-stats') || 'null');
      if (Array.isArray(raw)) {
        const valid = STAT_DEFS.map(d => d.key).filter(k => raw.includes(k));
        if (valid.length) return valid;
      }
    } catch (_) { /* ignore */ }
    return [...DEFAULT_STAT_METRICS];
  }

  function saveStatMetrics() {
    try {
      localStorage.setItem('dmm-stats', JSON.stringify(state.statMetrics));
    } catch (_) { /* ignore */ }
  }

  function renderStatsConfig() {
    const list = $('statsConfigList');
    if (!list) return;
    const selected = new Set(state.statMetrics || []);
    list.innerHTML = '';
    for (const def of STAT_DEFS) {
      const label = document.createElement('label');
      label.className = 'stats-check';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = def.key;
      cb.checked = selected.has(def.key);
      cb.addEventListener('change', () => {
        const set = new Set(state.statMetrics || []);
        if (cb.checked) set.add(def.key); else set.delete(def.key);
        state.statMetrics = STAT_DEFS.map(d => d.key).filter(k => set.has(k));
        saveStatMetrics();
        renderStats();
      });
      label.appendChild(cb);
      label.appendChild(document.createTextNode(def.label));
      list.appendChild(label);
    }
    if ($('statsConfigCount')) {
      $('statsConfigCount').textContent = `${(state.statMetrics || []).length} / ${STAT_DEFS.length} 项`;
    }
  }

  // ------------------------------------------------------------------
  // 控件状态
  // ------------------------------------------------------------------
  function fillSelect(sel, items, valueKey = 'code', labelKey = 'label') {
    if (!sel || sel.dataset.filled === '1') return;
    sel.innerHTML = '';
    for (const it of items) {
      const opt = document.createElement('option');
      opt.value = it[valueKey];
      opt.textContent = it[labelKey];
      sel.appendChild(opt);
    }
    sel.dataset.filled = '1';
  }

  // 量程选项随功能变化（电压/电阻/电流各自的量程表；固定量程功能只显示一项）
  function buildRangeOptions() {
    if (!state.meta) return;
    const sel = $('rangeSelect');
    const code = state.instrument.function || 'DCV';
    if (sel.dataset.mode === code) return;
    sel.dataset.mode = code;
    sel.innerHTML = '';
    const f = fnMeta(code);
    const fixed = fixedRange(code);
    const items = fixed
      ? [{ code: '__fixed', label: fixed + '（固定）' }]
      : (f && f.ranges && f.ranges.length ? f.ranges : state.meta.ranges);
    for (const it of items) {
      const opt = document.createElement('option');
      opt.value = it.code;
      opt.textContent = it.label;
      sel.appendChild(opt);
    }
  }

  function renderFunctionList() {
    if (!state.meta) return;
    const list = $('fnList');
    if (list.dataset.filled === '1') return;
    list.innerHTML = '';
    const byCode = new Map(state.meta.functions.map(f => [f.code, f]));
    for (const group of FN_GROUPS) {
      const head = document.createElement('div');
      head.className = 'fn-group-label';
      head.textContent = group.label;
      list.appendChild(head);
      for (const code of group.codes) {
        const f = byCode.get(code);
        if (!f) continue;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'fn-item';
        btn.dataset.code = code;
        btn.title = f.note || f.label;
        btn.innerHTML = `<span class="fn-name">${f.label}</span>`
          + `<span class="fn-unit">${f.unit}</span>`
          + `<span class="fn-code">${f.code}</span>`;
        btn.addEventListener('click', () => switchFunction(code));
        list.appendChild(btn);
      }
    }
    list.dataset.filled = '1';
  }

  function renderFunctionActive() {
    const active = state.instrument.function;
    const offline = (state.device.state || 'offline') !== 'online';
    for (const btn of $('fnList').querySelectorAll('.fn-item')) {
      btn.classList.toggle('active', btn.dataset.code === active);
      btn.classList.toggle('pending', state.switching && btn.dataset.code === active);
      btn.setAttribute('aria-pressed', btn.dataset.code === active ? 'true' : 'false');
      btn.disabled = offline;
    }
  }

  async function switchFunction(code) {
    if ((state.device.state || 'offline') !== 'online') {
      showToast('设备离线，无法切换功能', 'error');
      return;
    }
    if (code === state.instrument.function && !state.switching) return;
    if (state.hold) setHold(false, true);
    const token = ++state.switchSeq;
    const meta = fnMeta(code);
    const needExitShunt = state.config.display_mode === 'current' && code !== 'DCV';
    const prev = {
      instrument: { ...state.instrument },
      config: { ...state.config },
      samples: state.samples,
      latest: state.latest,
      stats: state.stats,
      alarm: state.alarm,
    };

    // 乐观切换：立刻显示目标功能页，等待仪器回读确认
    state.switching = true;
    state.instrument = {
      ...state.instrument,
      function: code,
      function_label: meta ? meta.label : code,
    };
    if (needExitShunt) {
      state.config = { ...state.config, display_mode: 'voltage' };
    }
    state.samples = [];
    state.latest = null;
    state.alarm = 'off';
    state.stats = {
      ctx: state.contextId, n: 0, n_total: 0, n_ol: 0,
      min: null, max: null, avg: null, pp: 0, std: 0,
      unit: meta ? meta.unit : 'V', since: Date.now() / 1000,
    };
    renderControls();
    renderReadout();
    renderStats();
    renderConfigUI();
    renderLimits(true);
    setAlarmBanner('off');
    chart.dirty = true;

    try {
      if (needExitShunt) {
        await postJSON('/api/config', {
          display_mode: 'voltage',
          shunt_ohm: state.config.shunt_ohm,
        });
      }
      const result = await postJSON('/api/control', { action: 'function', value: code });
      if (token !== state.switchSeq) return;
      if (result.applied && result.applied.function && result.applied.function !== code) {
        state.instrument = result.instrument;
        renderControls();
        renderReadout();
        renderConfigUI();
        renderLimits(true);
        showToast('仪器未接受功能切换，当前：' + (result.instrument.function_label || result.applied.function), 'error');
        return;
      }
      showToast('已切换到 ' + (meta ? meta.label : code), 'ok');
    } catch (err) {
      if (token !== state.switchSeq) return;
      state.instrument = prev.instrument;
      state.config = prev.config;
      state.samples = prev.samples;
      state.latest = prev.latest;
      state.stats = prev.stats;
      state.alarm = prev.alarm;
      renderControls();
      renderReadout();
      renderStats();
      renderConfigUI();
      renderLimits(true);
      renderFunctionActive();
      setAlarmBanner(prev.alarm);
      chart.dirty = true;
      showToast(err.message, 'error');
    } finally {
      if (token === state.switchSeq) {
        state.switching = false;
        renderFunctionActive();
        renderDevice();
        renderReadout();
      }
    }
  }

  function renderControls() {
    if (!state.meta) return;
    renderFunctionList();
    fillSelect($('rateSelect'), state.meta.rates);
    fillSelect($('triggerSelect'), state.meta.triggers);
    buildRangeOptions();

    const inst = state.instrument || {};
    const rangeSel = $('rangeSelect');
    if (fixedRange()) {
      rangeSel.value = '__fixed';
    } else if (inst.range && !rangeSel.classList.contains('pending')) {
      if (![...rangeSel.options].some(o => o.value === inst.range)) {
        const opt = document.createElement('option');
        opt.value = inst.range;
        opt.textContent = inst.range;
        rangeSel.appendChild(opt);
      }
      rangeSel.value = inst.range;
    }
    const notPending = (id) => !$(id).classList.contains('pending');
    if (inst.rate && notPending('rateSelect')) $('rateSelect').value = inst.rate;
    if (inst.trigger && notPending('triggerSelect')) $('triggerSelect').value = inst.trigger;

    renderFunctionActive();
    renderDevice();
    applyFunctionLayout();
  }

  // ---- 按功能显示/隐藏卡片与区块 ----------------------------------------
  function revealToggle(el, on, animate) {
    if (!el) return;
    const wasHidden = el.classList.contains('hidden');
    el.classList.toggle('hidden', !on);
    if (on && wasHidden && animate) restartClass(el, 'fn-reveal');
  }

  function fnAdvItems(code) {
    if (!adv.schema) return [];
    const out = [];
    for (const g of adv.schema) {
      for (const it of g.items) {
        if (it.key === 'cont_limit' || !Array.isArray(it.applies)) continue;
        if (it.applies.includes(code) && (it.kind === 'select' || it.kind === 'number')) out.push(it);
      }
    }
    return out;
  }

  function applyFunctionLayout(force = false) {
    if (!state.meta) return;
    const code = state.instrument.function || 'DCV';
    const shuntMode = state.config.display_mode === 'current';
    const nullOn = !!(state.null && state.null.enabled);
    const key = [code, shuntMode, nullOn, !!adv.schema, isOnline()].join('|');
    if (!force && state.layoutKey === key) return;
    const animate = state.layoutKey !== undefined;
    const fnChanged = !state.layoutKey || state.layoutKey.split('|')[0] !== code;
    state.layoutKey = key;

    const showShunt = hasFeature('shunt', code) || shuntMode;
    const showLimits = !!limitKey();
    revealToggle($('shuntCard'), showShunt, animate);
    revealToggle($('limitsCard'), showLimits, animate);
    const two = $('twoCol');
    two.classList.toggle('hidden', !showShunt && !showLimits);
    two.classList.toggle('single', showShunt !== showLimits);

    revealToggle($('itBlock'), hasFeature('integration', code), animate);
    // NULL 已开启时始终显示，方便关闭
    revealToggle($('nullBlock'), hasFeature('null', code) || nullOn, animate);

    const isCont = code === 'CONT';
    revealToggle($('contBlock'), isCont, animate);
    const items = fnAdvItems(code);
    if (fnChanged || force || !$('fnAdvList').children.length) renderFnAdvList(items);
    const meta = fnMeta(code);
    setText('fnSettingsTitle', (meta ? meta.label : code) + '设置');
    setText('fnSettingsCode', code);
    revealToggle($('fnSettingsCard'), isCont || items.length > 0, animate);
    if (isCont && (fnChanged || !state.contThreshold)) loadContThreshold();
    if (items.length && (fnChanged || force || !state.fnAdvLoaded)) loadFnAdvValues(items);
    renderCont();
  }

  function renderFnAdvList(items) {
    state.fnAdvLoaded = false;
    const list = $('fnAdvList');
    list.innerHTML = '';
    for (const it of items) {
      const row = document.createElement('div');
      row.className = 'form-row';
      const label = document.createElement('label');
      label.textContent = it.label.replace(/（.*?）/, '').replace(/^DCV\s*/, '');
      label.htmlFor = 'fnAdv_' + it.key;
      row.appendChild(label);
      if (it.kind === 'select') {
        const sel = document.createElement('select');
        sel.id = 'fnAdv_' + it.key;
        sel.dataset.key = it.key;
        const ph = document.createElement('option');
        ph.value = ''; ph.textContent = '读取中…'; ph.disabled = true; ph.selected = true;
        sel.appendChild(ph);
        for (const [val, text] of it.options || []) {
          const o = document.createElement('option');
          o.value = val; o.textContent = text;
          sel.appendChild(o);
        }
        sel.addEventListener('change', () => applyFnAdv(it, sel.value, sel));
        row.appendChild(sel);
      } else {
        const wrap = document.createElement('div');
        wrap.className = 'it-controls';
        const inp = document.createElement('input');
        inp.type = 'number'; inp.step = 'any'; inp.id = 'fnAdv_' + it.key;
        inp.dataset.key = it.key;
        if (it.hint) inp.placeholder = it.hint;
        const btn = document.createElement('button');
        btn.className = 'btn btn-ghost btn-sm'; btn.textContent = '应用';
        const go = () => {
          const v = parseFloat(inp.value);
          if (!Number.isFinite(v)) { markInvalid(inp, '请输入有效数字'); return; }
          withBusy(btn, () => applyFnAdv(it, v, inp));
        };
        btn.addEventListener('click', go);
        onEnter(inp, go);
        wrap.append(inp);
        if (it.unit) {
          const u = document.createElement('span');
          u.className = 'unit-fixed'; u.textContent = it.unit;
          wrap.append(u);
        }
        wrap.append(btn);
        row.appendChild(wrap);
      }
      list.appendChild(row);
    }
  }

  async function loadFnAdvValues(items) {
    if (!isOnline() || !items.length) return;
    try {
      const d = await api('/api/advanced/read?keys=' + encodeURIComponent(items.map(i => i.key).join(',')));
      state.fnAdvLoaded = true;
      for (const it of items) {
        const el = $('fnAdv_' + it.key);
        const v = d.values && d.values[it.key];
        if (!el || !v || !v.ok) continue;
        if (it.kind === 'select') {
          const m = (it.options || []).find(([code]) => String(v.value).toUpperCase().startsWith(code));
          if (m && !el.classList.contains('pending')) el.value = m[0];
        } else {
          const n = parseAdvNumber(v.value);
          if (n !== null && document.activeElement !== el) el.placeholder = '当前 ' + trimNum(n);
        }
      }
    } catch (_) { /* 回读失败时保持原样 */ }
  }

  async function applyFnAdv(item, value, el) {
    if (!isOnline()) { showToast('设备未连接', 'error'); return; }
    el.classList.add('pending');
    el.disabled = true;
    try {
      await postJSON('/api/advanced/set', { key: item.key, value });
      flashOk(el);
      const opt = item.kind === 'select' && (item.options || []).find(o => o[0] === value);
      showToast(item.label + ' 已设为 ' + (opt ? opt[1] : value + (item.unit || '')), 'ok');
    } catch (err) {
      showToast(err.message, 'error');
    } finally {
      el.classList.remove('pending');
      el.disabled = false;
      loadFnAdvValues([item]);
    }
  }

  // ---- 通断阈值 --------------------------------------------------------
  function parseKom(v) {
    const m = String(v || '').match(/KOM\s*\+?(\d+(?:\.\d+)?)/i) || String(v || '').match(/^\s*\+?(\d+(?:\.\d+)?)/);
    return m ? Math.round(parseFloat(m[1])) : null;
  }

  async function loadContThreshold() {
    if (!isOnline()) return;
    try {
      const d = await api('/api/advanced/read?keys=cont_limit');
      const v = d.values && d.values.cont_limit;
      if (v && v.ok) {
        const n = parseKom(v.value);
        if (n !== null) state.contThreshold = n;
      }
    } catch (_) { /* 保留缓存值 */ }
    renderCont();
    renderReadout();
  }

  function renderCont() {
    const thr = state.contThreshold;
    setText('contReadback', thr ? `当前：${thr} Ω` : '当前：—');
    const online = isOnline();
    for (const b of $('contPresets').querySelectorAll('button')) {
      b.classList.toggle('active', Number(b.dataset.v) === thr);
      b.disabled = !online;
    }
    if (!$('contApply').classList.contains('is-busy')) $('contApply').disabled = !online;
    $('contValue').disabled = !online;
    if (thr && document.activeElement !== $('contValue')) $('contValue').placeholder = String(thr);
  }

  async function applyContThreshold(raw) {
    const input = $('contValue');
    const v = Number(raw);
    if (!Number.isFinite(v) || v < 1 || v > 1000 || Math.round(v) !== v) {
      markInvalid(input, '阈值为 1 – 1000 的整数 Ω');
      return;
    }
    if (!isOnline()) { showToast('设备未连接，无法设置通断阈值', 'error'); return; }
    await withBusy($('contApply'), async () => {
      try {
        const d = await postJSON('/api/advanced/set', { key: 'cont_limit', value: v });
        state.contThreshold = parseKom(d.value) || v;
        input.value = '';
        flashOk(input);
        showToast(`通断阈值已设为 ${state.contThreshold} Ω`, 'ok');
        if (advIsOpen()) loadAdvKeys(['cont_limit']);
      } catch (err) {
        showToast(err.message, 'error');
      }
    });
    renderCont();
    renderReadout();
  }

  $('contApply').addEventListener('click', () => applyContThreshold($('contValue').value));
  onEnter($('contValue'), () => applyContThreshold($('contValue').value));
  $('contPresets').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-v]');
    if (b) applyContThreshold(b.dataset.v);
  });

  // ---- 完全释放控制 ----------------------------------------------------
  function renderRelease() {
    const released = isReleased() && state.serverOnline;
    const btn = $('releaseBtn');
    btn.classList.toggle('is-released', released);
    setText('releaseText', released ? '恢复控制' : '释放控制');
    btn.title = released ? '恢复上位机控制 (P)' : '完全释放控制给仪器面板 (P)';
    if (!btn.classList.contains('is-busy')) {
      btn.disabled = !state.serverOnline || !(released || isOnline());
    }
    $('releaseBanner').classList.toggle('hidden', !released);
    document.body.classList.toggle('is-released', released);
    if (released) {
      // 仪器命令全部锁定；分流电阻与限值报警只在上位机计算，仍可修改
      $('controlHint').textContent = '已释放给仪器面板，点「恢复控制」后才能在网页上设置仪器。';
      for (const id of ['contApply', 'contValue']) {
        const el = $(id);
        if (el && !el.classList.contains('is-busy')) el.disabled = true;
      }
      for (const b of $('contPresets').querySelectorAll('button')) b.disabled = true;
      for (const el of $('fnAdvList').querySelectorAll('select, input, button')) el.disabled = true;
    } else {
      for (const el of $('fnAdvList').querySelectorAll('select, input, button')) {
        if (!el.classList.contains('pending')) el.disabled = !isOnline();
      }
    }
  }

  async function toggleRelease() {
    if (!state.serverOnline) return;
    const btn = $('releaseBtn');
    const releasing = !isReleased();
    if (releasing && !isOnline()) { showToast('设备未连接', 'error'); return; }
    await withBusy(btn, async () => {
      try {
        const d = await postJSON(releasing ? '/api/device/release' : '/api/device/resume', {});
        if (d.state) applyState(d.state);
        showToast(releasing ? '已释放给仪器面板，网页只接收数据' : '已恢复上位机控制', 'ok');
        if (!releasing) { loadIntegrationTime(); state.layoutKey = undefined; applyFunctionLayout(true); }
      } catch (err) {
        showToast(err.message, 'error');
      }
    });
    renderRelease();
  }

  $('releaseBtn').addEventListener('click', toggleRelease);
  $('resumeBtn').addEventListener('click', toggleRelease);

  function renderConfigUI() {
    const mode = state.config.display_mode;
    for (const btn of $('modeGroup').querySelectorAll('button')) {
      btn.classList.toggle('active', btn.dataset.mode === mode);
    }
    if (!state.shuntDirty) {
      const ohm = state.config.shunt_ohm;
      if (ohm >= 1) { $('shuntValue').value = trimNum(ohm); $('shuntUnit').value = '1'; }
      else { $('shuntValue').value = trimNum(ohm * 1000); $('shuntUnit').value = '0.001'; }
    }
    updateTitles();
    const key = limitKey();
    $('limitModeLabel').textContent = key === 'current' ? '作用量：电流'
      : key === 'voltage' ? '作用量：电压' : '作用量：—';
    renderDirty();
    applyFunctionLayout();
  }

  // 未应用的修改：输入框描边 + 按钮呼吸提示
  function renderDirty() {
    const sd = !!state.shuntDirty;
    $('shuntDirtyTag').classList.toggle('hidden', !sd);
    $('applyShunt').classList.toggle('pending-apply', sd);
    $('shuntValue').classList.toggle('dirty', sd);
    const ld = !!state.limitsDirty && !!limitKey();
    $('limitsDirtyTag').classList.toggle('hidden', !ld);
    $('applyLimits').classList.toggle('pending-apply', ld);
    for (const id of ['limitLow', 'limitHigh', 'limitEnabled']) $(id).classList.toggle('dirty', ld);
  }

  function updateTitles() {
    const u = displayUnit();
    const name = UNIT_NAMES[u] || '读数';
    $('chartTitle').textContent = `实时${name}趋势 (${u})`;
    $('tableUnitHead').textContent = `值 (${u})`;
  }

  function trimNum(v) {
    if (!Number.isFinite(v)) return '';
    return String(Number(v.toPrecision(10)));
  }

  function limitUnits(mode) {
    return mode === 'current'
      ? [['1', 'A'], ['0.001', 'mA'], ['0.000001', 'µA']]
      : [['1', 'V'], ['0.001', 'mV'], ['0.000001', 'µV']];
  }

  function renderLimits(force = false) {
    const key = limitKey();
    const supported = !!key;
    const unitKey = key === 'current' ? 'current' : 'voltage';
    if (force || state.limitsMode !== (key || 'none')) {
      state.limitsMode = key || 'none';
      state.limitsDirty = false;
      for (const id of ['limitLowUnit', 'limitHighUnit']) {
        const sel = $(id);
        sel.innerHTML = '';
        for (const [val, label] of limitUnits(unitKey)) {
          const o = document.createElement('option');
          o.value = val; o.textContent = label;
          sel.appendChild(o);
        }
        sel.value = '1';
      }
    }
    for (const id of ['limitEnabled', 'limitLow', 'limitHigh', 'limitLowUnit', 'limitHighUnit', 'applyLimits']) {
      $(id).disabled = !supported;
    }
    $('limitHint').textContent = supported
      ? '限值作用于当前显示量，超限时读数与曲线同步提示。'
      : '当前功能（电阻/频率）不支持软件限值报警。';
    if (!supported) {
      $('limitEnabled').checked = false;
      $('limitLow').value = '';
      $('limitHigh').value = '';
      $('limitState').textContent = '不支持';
      $('limitState').className = 'tag';
      renderDirty();
      return;
    }
    const lim = (state.limits || {})[key] || {};
    if (!state.limitsDirty) {
      $('limitEnabled').checked = !!lim.enabled;
      $('limitLow').value = lim.low === null || lim.low === undefined ? '' : trimNum(lim.low);
      $('limitHigh').value = lim.high === null || lim.high === undefined ? '' : trimNum(lim.high);
    }
    renderDirty();
    updateLimitTag(state.alarm);
  }

  function renderLogging() {
    const lg = state.logging || {};
    const active = !!lg.active;
    $('logIndicator').classList.toggle('hidden', !active);
    document.querySelector('.log-card').classList.toggle('recording', active);
    if (!$('logStart').classList.contains('is-busy')) $('logStart').disabled = active;
    if (!$('logStop').classList.contains('is-busy')) $('logStop').disabled = !active;
    const status = $('logStatus');
    status.textContent = active ? '记录中'
      : (lg.file ? '已停止' : '未记录');
    status.className = 'tag' + (active ? ' rec' : '') + (lg.error ? ' alarm' : '');
    if (lg.error) status.textContent += ' · 写入错误';
    setText('logRows', String(lg.rows || 0));
    $('logFileInfo').textContent = lg.file
      ? `${active ? '正在写入' : '最近文件'}：${lg.file}`
      : 'CSV 保存到 data/logs/';
    renderLogClock();
  }

  function renderLogClock() {
    const lg = state.logging || {};
    if (lg.active && lg.started_at) {
      const el = Date.now() / 1000 - lg.started_at;
      setText('logElapsed', fmtClock(el));
      const c = fmtClock(el);
      setText('logElapsedTop', c.startsWith('00:') ? c.slice(3) : c);
    } else if (!lg.file) {
      setText('logElapsed', '00:00:00');
    }   // 停止后保留最后一次的时长
  }

  // ------------------------------------------------------------------
  // 状态应用
  // ------------------------------------------------------------------
  function applyState(d) {
    if (!d) return;
    if (typeof d.sim === 'boolean') state.sim = d.sim;
    if (d.device) state.device = d.device;
    if (d.instrument && (!state.switching || !d.instrument.function
        || d.instrument.function === state.instrument.function)) {
      state.instrument = d.instrument;
    }
    if (d.config) state.config = d.config;
    if (d.limits) state.limits = d.limits;
    if (d.null) state.null = d.null;
    if (typeof d.released === 'boolean') state.released = d.released;
    if (d.cont_threshold !== undefined && d.cont_threshold !== null) state.contThreshold = d.cont_threshold;
    if (typeof d.alarm === 'string' && !state.switching) state.alarm = d.alarm;
    if (d.logging) state.logging = d.logging;
    if (d.stats && !state.switching) state.stats = d.stats;
    if (typeof d.context_id === 'number' && d.context_id !== state.contextId) {
      state.contextId = d.context_id;
      state.samples = [];
      state.latest = null;
      state.lastSeq = 0;
      state.view = { live: true, start: 0, end: 0 };
      setZoomUI();
      chart.dirty = true;
    }
    renderDevice();
    renderControls();
    renderConfigUI();
    renderLimits();
    renderLogging();
    renderStats();
    renderReadout();
    renderNull();
    applyFunctionLayout();
    chart.dirty = true;
    if (adv.schema && advIsOpen()
        && adv.fn !== state.instrument.function) {
      renderAdvanced();
    }
  }

  function applySnapshot(d) {
    applyState(d);
    state.contextId = d.context_id;
    state.samples = Array.isArray(d.samples) ? d.samples.slice() : [];
    state.latest = state.samples.length ? state.samples[state.samples.length - 1] : null;
    state.lastSeq = state.latest ? state.latest.seq : 0;
    chart.dirty = true;
    if (state.latest) renderReadout(state.latest);
    if (state.windowSec === 0 || state.windowSec > 60) fetchHistory();
  }

  function onSample(s) {
    if (state.switching) {
      const target = state.instrument.function;
      const mismatch = s.fn && target && s.fn !== target;
      const stale = s.ctx === state.contextId && !s.fn;
      if (mismatch || stale) return;   // 切换过程中丢弃旧功能的数据
    }
    if (s.ctx !== state.contextId) {
      state.contextId = s.ctx;
      state.samples = [];
      state.view = { live: true, start: 0, end: 0 };
      setZoomUI();
      chart.dirty = true;
    }
    state.samples.push(s);
    if (state.samples.length > 60000) state.samples.splice(0, state.samples.length - 60000);
    state.latest = s;
    state.lastSeq = s.seq;
    state.alarm = s.alarm;
    state.lastSampleAt = Date.now();
    state.readoutDirty = true;
    state.activityPending = true;
    chart.dirty = true;
  }

  // ------------------------------------------------------------------
  // SSE
  // ------------------------------------------------------------------
  function connectStream() {
    const es = new EventSource('/api/stream');
    es.onopen = () => { state.serverOnline = true; renderDevice(); };
    es.onerror = () => { state.serverOnline = false; renderDevice(); };
    es.addEventListener('snapshot', (e) => { state.serverOnline = true; applySnapshot(JSON.parse(e.data)); });
    es.addEventListener('sample', (e) => onSample(JSON.parse(e.data)));
    es.addEventListener('samples', (e) => {
      const batch = JSON.parse(e.data);
      if (!Array.isArray(batch)) return;
      for (const s of batch) onSample(s);
    });
    es.addEventListener('state', (e) => applyState(JSON.parse(e.data)));
    es.addEventListener('stats', (e) => {
      if (state.switching) return;
      state.stats = JSON.parse(e.data);
      renderStats();
    });
    es.addEventListener('log', (e) => { state.logging = JSON.parse(e.data); renderLogging(); });
    es.addEventListener('status', (e) => {
      const d = JSON.parse(e.data);
      if (d.device) state.device = d.device;
      renderDevice();
    });
  }

  function currentDomain() {
    const latestT = state.latest ? state.latest.t : Date.now() / 1000;
    if (!state.view.live && state.view.end > state.view.start) {
      return { tMin: state.view.start, tMax: state.view.end, free: true };
    }
    if (state.windowSec > 0) {
      return { tMin: latestT - state.windowSec, tMax: latestT, free: false };
    }
    let firstT = null;
    for (const s of state.samples) {
      if (s.ctx === state.contextId) { firstT = s.t; break; }
    }
    const tMin = firstT !== null ? Math.min(firstT, latestT - 1) : latestT - 60;
    return { tMin, tMax: Math.max(latestT, tMin + 1), free: false };
  }

  function oldestLoadedT() {
    for (const s of state.samples) {
      if (s.ctx === state.contextId) return s.t;
    }
    return Infinity;
  }

  let historyTimer = null;
  function ensureHistory() {
    clearTimeout(historyTimer);
    historyTimer = setTimeout(() => {
      if (!state.view.live && state.view.start < oldestLoadedT() - 0.5) {
        fetchHistory(0);
      }
    }, 350);
  }

  async function fetchHistory(seconds = null) {
    try {
      const q = seconds === null ? (state.windowSec || 0) : seconds;
      const d = await api(`/api/history?seconds=${q}`);
      if (d.context_id !== state.contextId) return;
      const map = new Map(state.samples.map(s => [s.seq, s]));
      for (const s of d.samples || []) map.set(s.seq, s);
      state.samples = [...map.values()].sort((a, b) => a.seq - b.seq);
      state.latest = state.samples.length ? state.samples[state.samples.length - 1] : null;
      chart.dirty = true;
    } catch (_) { /* 忽略历史拉取失败 */ }
  }

  // ------------------------------------------------------------------
  // 图表
  // ------------------------------------------------------------------
  const chart = { canvas: null, ctx: null, dpr: 1, w: 0, h: 0, dirty: true, cross: null, visible: [] };

  function cssVar(name) {
    return getComputedStyle(document.body).getPropertyValue(name).trim();
  }

  // 颜色只在主题/显示模式变化时重新读取，避免每帧 getComputedStyle
  function chartColors() {
    const key = state.theme + '|' + state.config.display_mode;
    if (chart.colors && chart.colorsKey === key) return chart.colors;
    chart.colorsKey = key;
    chart.colors = {
      ink: cssVar('--ink-2'), muted: cssVar('--muted'), grid: cssVar('--grid'),
      axis: cssVar('--axis'), panel: cssVar('--panel-2'),
      accent: state.config.display_mode === 'current' ? cssVar('--accent-current') : cssVar('--accent'),
      bad: cssVar('--bad'), warn: cssVar('--warn'),
    };
    return chart.colors;
  }

  // 交互期间（拖动/缩放/悬停）按显示器刷新率重绘
  function markInteract(ms = 600) {
    chart.interactUntil = performance.now() + ms;
    chart.dirty = true;
  }

  // 把 #rrggbb 颜色加上透明度
  function withAlpha(color, a) {
    const m = /^#([0-9a-f]{6})$/i.exec(color || '');
    if (!m) return color;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }

  function setupChart() {
    chart.canvas = $('chart');
    chart.ctx = chart.canvas.getContext('2d');
    const resize = () => {
      const rect = chart.canvas.getBoundingClientRect();
      chart.dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      chart.w = Math.max(1, rect.width);
      chart.h = Math.max(1, rect.height);
      chart.canvas.width = Math.round(chart.w * chart.dpr);
      chart.canvas.height = Math.round(chart.h * chart.dpr);
      chart.ctx.setTransform(chart.dpr, 0, 0, chart.dpr, 0, 0);
      chart.dirty = true;
    };
    new ResizeObserver(resize).observe(chart.canvas);
    resize();

    let drag = null;

    chart.canvas.addEventListener('mousemove', (ev) => {
      if (drag || !chart.visible.length) return;
      const rect = chart.canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      // px 单调递增，二分查找最近点
      let lo = 0, hi = chart.visible.length - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (chart.visible[mid].px < x) lo = mid + 1;
        else hi = mid;
      }
      let best = lo;
      if (lo > 0 && Math.abs(chart.visible[lo - 1].px - x) < Math.abs(chart.visible[lo].px - x)) {
        best = lo - 1;
      }
      const bestD = Math.abs(chart.visible[best].px - x);
      if (bestD <= 40) {
        if (chart.cross !== best) { chart.cross = best; markInteract(300); }
      } else if (chart.cross !== null) { chart.cross = null; markInteract(300); }
    });
    chart.canvas.addEventListener('mouseleave', () => { chart.cross = null; chart.dirty = true; });
    chart.canvas.addEventListener('keydown', (ev) => {
      if (!chart.visible.length) return;
      if (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight') {
        ev.preventDefault();
        let idx = chart.cross === null ? chart.visible.length - 1 : chart.cross;
        idx += ev.key === 'ArrowLeft' ? -1 : 1;
        chart.cross = Math.max(0, Math.min(chart.visible.length - 1, idx));
        chart.dirty = true;
      } else if (ev.key === 'Escape') {
        chart.cross = null; chart.dirty = true;
      }
    });

    // 滚轮缩放时间轴（以鼠标位置为中心）
    chart.canvas.addEventListener('wheel', (ev) => {
      if (!chart.geom) return;
      ev.preventDefault();
      const rect = chart.canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const g = chart.geom;
      const dom = currentDomain();
      const ratio = Math.min(1, Math.max(0, (x - g.l) / g.plotW));
      const cursorT = dom.tMin + ratio * (dom.tMax - dom.tMin);
      const span = dom.tMax - dom.tMin;
      const factor = Math.exp(ev.deltaY * 0.0015);
      const newSpan = Math.min(3600, Math.max(0.2, span * factor));
      const newTMin = cursorT - ratio * newSpan;
      state.view = { live: false, start: newTMin, end: newTMin + newSpan };
      chart.momentum = null;
      setZoomUI();
      chart.cross = null;
      markInteract();
      ensureHistory();
    }, { passive: false });

    // 拖动平移
    chart.canvas.addEventListener('pointerdown', (ev) => {
      if (ev.button !== 0 || !chart.geom) return;
      chart.momentum = null;
      drag = {
        x: ev.clientX,
        tMin: chart.geom.tMin,
        tMax: chart.geom.tMax,
        plotW: chart.geom.plotW,
        moved: false,
        hist: [{ x: ev.clientX, t: performance.now() }],
      };
      chart.cross = null;
      chart.dirty = true;
      try { chart.canvas.setPointerCapture(ev.pointerId); } catch (_) {}
      chart.canvas.style.cursor = 'grabbing';
      ev.preventDefault();
    });
    chart.canvas.addEventListener('pointermove', (ev) => {
      if (!drag) return;
      const dx = ev.clientX - drag.x;
      if (!drag.moved && Math.abs(dx) < 3) return;
      drag.moved = true;
      const span = drag.tMax - drag.tMin;
      const dt = -(dx / drag.plotW) * span;
      state.view = { live: false, start: drag.tMin + dt, end: drag.tMax + dt };
      const nowT = performance.now();
      drag.hist.push({ x: ev.clientX, t: nowT });
      while (drag.hist.length > 2 && nowT - drag.hist[0].t > 100) drag.hist.shift();
      setZoomUI();
      markInteract();
      ensureHistory();
    });
    const endDrag = (ev) => {
      if (!drag) return;
      // 松手惯性：用最后 100ms 的拖动速度继续滑动并减速
      if (drag.moved && ev.type === 'pointerup' && !REDUCED_MOTION) {
        const h = drag.hist;
        const a = h[0], b = h[h.length - 1];
        const dtMs = b.t - a.t;
        if (dtMs > 0 && performance.now() - b.t < 80) {
          const pxPerMs = (b.x - a.x) / dtMs;
          if (Math.abs(pxPerMs) > 0.15) {
            const span = drag.tMax - drag.tMin;
            chart.momentum = { v: -(pxPerMs / drag.plotW) * span, last: performance.now() };
            markInteract(1500);
          }
        }
      }
      drag = null;
      chart.canvas.style.cursor = '';
      try { chart.canvas.releasePointerCapture(ev.pointerId); } catch (_) {}
    };
    chart.canvas.addEventListener('pointerup', endDrag);
    chart.canvas.addEventListener('pointercancel', endDrag);
    chart.canvas.addEventListener('dblclick', (ev) => {
      ev.preventDefault();
      resetZoomView();
    });
  }

  function stepMomentum(now) {
    const m = chart.momentum;
    if (!m) return;
    const dt = Math.min(50, now - m.last);
    m.last = now;
    if (state.view.live) { chart.momentum = null; return; }
    const shift = m.v * dt;
    state.view = { live: false, start: state.view.start + shift, end: state.view.end + shift };
    m.v *= Math.pow(0.995, dt);   // 指数衰减
    const span = state.view.end - state.view.start;
    if (Math.abs(m.v * 16) < span * 0.0005) {
      chart.momentum = null;
      ensureHistory();
    }
    markInteract(200);
  }

  function setZoomUI() {
    const live = state.view.live;
    const liveBtn = $('zoomLive');
    if (liveBtn) liveBtn.classList.toggle('active', live);
    for (const b of $('windowGroup').querySelectorAll('button')) {
      b.classList.toggle('active', live && parseInt(b.dataset.window, 10) === state.windowSec);
    }
    if (liveBtn) liveBtn.classList.toggle('attn', !live);
    const hint = $('zoomHint');
    if (hint) {
      hint.textContent = live ? '滚轮缩放 · 拖动平移 · 双击复位' : '已暂停跟随 · 双击或按 L 回到实时';
      hint.classList.toggle('zoomed', !live);
    }
  }

  function resetZoomView() {
    chart.momentum = null;
    state.view = { live: true, start: 0, end: 0 };
    setZoomUI();
    chart.dirty = true;
    if (state.windowSec === 0 || state.windowSec > 60) fetchHistory();
  }

  function zoomBy(factor) {
    if (!chart.geom) return;
    const dom = currentDomain();
    const mid = (dom.tMin + dom.tMax) / 2;
    const span = Math.max(0.2, Math.min(3600, (dom.tMax - dom.tMin) * factor));
    state.view = { live: false, start: mid - span / 2, end: mid + span / 2 };
    chart.momentum = null;
    setZoomUI();
    chart.cross = null;
    markInteract();
    ensureHistory();
  }

  function niceStep(span, count) {
    if (!(span > 0) || !Number.isFinite(span)) return 1;
    const raw = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    let step;
    if (norm <= 1) step = 1; else if (norm <= 2) step = 2; else if (norm <= 5) step = 5; else step = 10;
    return step * mag;
  }

  function lowerBound(arr, t) {
    let lo = 0, hi = arr.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (arr[mid].t < t) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  function decimate(points, maxPoints) {
    if (points.length <= maxPoints) return points;
    const bucket = Math.ceil(points.length / Math.max(1, maxPoints / 2));
    const out = [];
    for (let i = 0; i < points.length; i += bucket) {
      const end = Math.min(points.length, i + bucket);
      let lo = null, hi = null, hasOl = false;
      for (let j = i; j < end; j++) {
        const p = points[j];
        if (p.d === null || p.d === undefined) { hasOl = true; continue; }
        if (!lo || p.d < lo.d) lo = p;
        if (!hi || p.d > hi.d) hi = p;
      }
      // 保留 OL 断点，避免把超量程区间连成假数据
      if (hasOl) {
        const f = points[i];
        out.push({ t: f.t, d: null, ol: true, u: f.u, fn: f.fn, raw: 'OL' });
      }
      if (lo && hi) {
        // 按时间顺序输出，不能固定先 min 后 max，否则会画出假竖线
        if (lo === hi) out.push(lo);
        else if (lo.t <= hi.t) { out.push(lo); out.push(hi); }
        else { out.push(hi); out.push(lo); }
      } else if (lo) {
        out.push(lo);
      }
    }
    return out;
  }

  function drawChart() {
    const canvas = chart.canvas, ctx = chart.ctx;
    if (!canvas || !ctx) return;
    const W = chart.w, H = chart.h;
    ctx.clearRect(0, 0, W, H);

    const colors = chartColors();
    const unit = baseUnit();
    const emptyEl = $('chartEmpty');

    const dom = currentDomain();
    const tMin = dom.tMin;
    const tMax = Math.max(dom.tMax, dom.tMin + 1e-6);
    const marg = { l: 64, r: 16, t: 14, b: 26 };
    const plotW = Math.max(1, W - marg.l - marg.r);
    const plotH = Math.max(1, H - marg.t - marg.b);
    chart.geom = { l: marg.l, plotW, tMin, tMax };
    const xOf = (t) => marg.l + ((t - tMin) / (tMax - tMin)) * plotW;

    const arr = state.samples;
    const startIdx = lowerBound(arr, tMin);
    const endIdx = lowerBound(arr, tMax + 1e-9);
    let pts = arr.slice(startIdx, endIdx);
    if (!pts.length) {
      emptyEl.textContent = dom.free ? '该时间段没有数据（拖动或缩小查看）'
        : (isOnline() ? '等待数据…' : '设备未连接');
      emptyEl.classList.toggle('static', dom.free || !isOnline());
      emptyEl.classList.remove('hidden');
      chart.visible = [];
      return;
    }
    emptyEl.classList.add('hidden');

    const numeric = pts.filter(p => p.d !== null && p.d !== undefined && Number.isFinite(p.d));
    if (!numeric.length) {
      emptyEl.textContent = '当前超量程（OL），等待有效数据…';
      emptyEl.classList.add('static');
      emptyEl.classList.remove('hidden');
      chart.visible = [];
      return;
    }

    let yMin = Infinity, yMax = -Infinity;
    for (const p of numeric) { if (p.d < yMin) yMin = p.d; if (p.d > yMax) yMax = p.d; }
    if (yMin === yMax) {
      const pad = Math.max(Math.abs(yMin) * 0.001, 1e-9);
      yMin -= pad; yMax += pad;
    } else {
      const pad = (yMax - yMin) * 0.06;
      yMin -= pad; yMax += pad;
    }

    const yOf = (v) => marg.t + (1 - (v - yMin) / (yMax - yMin)) * plotH;

    // OL 区间底色（超量程时折线断开，这里给出明确提示）
    const olRuns = [];
    let runStart = null;
    for (const p of pts) {
      const isOl = p.ol || p.d === null || p.d === undefined;
      if (isOl && runStart === null) runStart = p.t;
      else if (!isOl && runStart !== null) { olRuns.push([runStart, p.t]); runStart = null; }
    }
    if (runStart !== null) olRuns.push([runStart, tMax]);
    if (olRuns.length) {
      ctx.save();
      ctx.globalAlpha = 0.13;
      ctx.fillStyle = colors.bad;
      for (const [a, b] of olRuns) {
        const x1 = xOf(a), x2 = xOf(b);
        ctx.fillRect(Math.min(x1, x2), marg.t, Math.max(1, Math.abs(x2 - x1)), plotH);
      }
      ctx.restore();
      ctx.fillStyle = colors.bad;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      ctx.font = '11px system-ui, "Noto Sans CJK SC", sans-serif';
      ctx.fillText(`OL 区间 ${olRuns.length} 段`, marg.l + 6, marg.t + 4);
    }

    // 网格与 Y 轴
    ctx.font = '11px system-ui, "Noto Sans CJK SC", sans-serif';
    ctx.textBaseline = 'middle';
    const step = niceStep(yMax - yMin, 5);
    const first = Math.ceil(yMin / step) * step;
    ctx.lineWidth = 1;
    for (let v = first; v <= yMax; v += step) {
      const y = Math.round(yOf(v)) + 0.5;
      ctx.strokeStyle = colors.grid;
      ctx.beginPath(); ctx.moveTo(marg.l, y); ctx.lineTo(W - marg.r, y); ctx.stroke();
      const lab = fmtEng(v, unit, 4);
      ctx.fillStyle = colors.muted;
      ctx.textAlign = 'right';
      ctx.fillText(lab.text + ' ' + lab.unit, marg.l - 8, y);
    }

    // X 轴
    ctx.strokeStyle = colors.axis;
    ctx.beginPath();
    ctx.moveTo(marg.l, marg.t + plotH + 0.5);
    ctx.lineTo(W - marg.r, marg.t + plotH + 0.5);
    ctx.stroke();
    ctx.fillStyle = colors.muted;
    ctx.textAlign = 'center';
    if (!dom.free && state.windowSec > 0) {
      for (const frac of [0, 0.25, 0.5, 0.75, 1]) {
        const sec = -(tMax - tMin) * (1 - frac);
        const x = marg.l + frac * plotW;
        ctx.fillText(sec === 0 ? '0' : `${Math.round(sec)}s`, x, marg.t + plotH + 14);
      }
    } else {
      const shortSpan = (tMax - tMin) < 2;
      for (const frac of [0, 0.25, 0.5, 0.75, 1]) {
        const t = tMin + (tMax - tMin) * frac;
        const lab = shortSpan ? timeStr(t)
          : new Date(t * 1000).toLocaleTimeString('zh-CN', { hour12: false });
        ctx.fillText(lab, marg.l + frac * plotW, marg.t + plotH + 14);
      }
    }

    // 限值线
    const lk = limitKey();
    const lim = lk ? ((state.limits || {})[lk] || {}) : {};
    if (lk && lim.enabled) {
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.lineWidth = 1;
      for (const [val, label, color] of [[lim.low, '下限', colors.warn], [lim.high, '上限', colors.bad]]) {
        if (val === null || val === undefined) continue;
        if (val < yMin || val > yMax) continue;
        const y = Math.round(yOf(val)) + 0.5;
        ctx.strokeStyle = color;
        ctx.beginPath(); ctx.moveTo(marg.l, y); ctx.lineTo(W - marg.r, y); ctx.stroke();
        ctx.fillStyle = color;
        ctx.textAlign = 'left';
        ctx.fillText(label, marg.l + 4, y - 7);
      }
      ctx.restore();
    }

    // 数据线（OL / 断档处断开）
    const drawPts = decimate(pts, Math.max(1000, Math.round(plotW * 2)));
    let gap = 1.0;
    if (pts.length > 2) {
      const diffs = [];
      const stride = Math.max(1, Math.floor((pts.length - 1) / 256));
      for (let i = 1; i < pts.length; i += stride) diffs.push(pts[i].t - pts[i - 1].t);
      diffs.sort((a, b) => a - b);
      const med = diffs[Math.floor(diffs.length / 2)];
      if (med > 0) gap = Math.max(0.5, med * 4);
    }
    // 按连续段收集坐标（OL / 断档处断开）
    const segs = [];
    let cur = null, prev = null;
    for (const p of drawPts) {
      if (p.d === null || p.d === undefined || !Number.isFinite(p.d)) {
        prev = null; cur = null;
        continue;
      }
      const x = xOf(p.t), y = yOf(p.d);
      if (prev === null || p.t - prev > gap || !cur) { cur = []; segs.push(cur); }
      cur.push(x, y);
      prev = p.t;
    }

    // 面积渐变填充
    const baseY = marg.t + plotH;
    const grad = ctx.createLinearGradient(0, marg.t, 0, baseY);
    grad.addColorStop(0, withAlpha(colors.accent, 0.20));
    grad.addColorStop(1, withAlpha(colors.accent, 0));
    ctx.fillStyle = grad;
    for (const seg of segs) {
      if (seg.length < 4) continue;
      ctx.beginPath();
      ctx.moveTo(seg[0], baseY);
      for (let i = 0; i < seg.length; i += 2) ctx.lineTo(seg[i], seg[i + 1]);
      ctx.lineTo(seg[seg.length - 2], baseY);
      ctx.closePath();
      ctx.fill();
    }

    ctx.strokeStyle = colors.accent;
    ctx.lineWidth = 1.75;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.beginPath();
    for (const seg of segs) {
      ctx.moveTo(seg[0], seg[1]);
      if (seg.length === 2) ctx.lineTo(seg[0] + 0.01, seg[1]);
      for (let i = 2; i < seg.length; i += 2) ctx.lineTo(seg[i], seg[i + 1]);
    }
    ctx.stroke();

    // 最新点
    const last = numeric[numeric.length - 1];
    const lx = xOf(last.t), ly = yOf(last.d);
    if (lx >= marg.l - 1 && lx <= W - marg.r + 1) {
      ctx.fillStyle = withAlpha(colors.accent, 0.22);
      ctx.beginPath(); ctx.arc(lx, ly, 8, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = colors.panel;
      ctx.beginPath(); ctx.arc(lx, ly, 5, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = colors.accent;
      ctx.beginPath(); ctx.arc(lx, ly, 3.5, 0, Math.PI * 2); ctx.fill();
    }

    // 十字光标使用降采样后的点，避免高速采样时每帧处理上万点
    chart.visible = drawPts.map(p => ({
      px: xOf(p.t),
      py: (p.d === null || p.d === undefined) ? null : yOf(p.d),
      s: p,
    }));

    // 十字光标
    const tip = $('chartTip');
    if (chart.cross !== null && chart.visible[chart.cross]) {
      const cp = chart.visible[chart.cross];
      ctx.strokeStyle = colors.axis;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cp.px + 0.5, marg.t); ctx.lineTo(cp.px + 0.5, marg.t + plotH); ctx.stroke();
      if (cp.py !== null) {
        ctx.fillStyle = colors.ink;
        ctx.beginPath(); ctx.arc(cp.px, cp.py, 3, 0, Math.PI * 2); ctx.fill();
      }
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = colors.axis;
      if (cp.py !== null) {
        ctx.beginPath(); ctx.moveTo(marg.l, cp.py + 0.5); ctx.lineTo(W - marg.r, cp.py + 0.5); ctx.stroke();
      }
      ctx.setLineDash([]);
      const v = cp.s.d;
      let txt = 'OL';
      if (v !== null && v !== undefined) { const f = fmtEng(v, unit, 6); txt = f.text + ' ' + f.unit; }
      tip.innerHTML = `<div class="tip-v">${txt}</div><div class="tip-t">${timeStr(cp.s.t)}</div>`;
      tip.classList.remove('hidden');
      const tw = tip.offsetWidth, th = tip.offsetHeight;
      let left = cp.px + 12, top = (cp.py !== null ? cp.py : marg.t) - th - 10;
      if (left + tw > W - 4) left = cp.px - tw - 12;
      if (top < 4) top = 4;
      tip.style.left = left + 'px';
      tip.style.top = top + 'px';
    } else {
      tip.classList.add('hidden');
    }
  }

  function chartTick() {
    const now = performance.now();
    if (state.activityPending && now - (chart.lastBlink || 0) >= 140 && !document.hidden) {
      state.activityPending = false;
      chart.lastBlink = now;
      const led = $('activityLed');
      led.classList.add('blink');
      setTimeout(() => led.classList.remove('blink'), 70);
    }
    if (state.readoutDirty && !document.hidden && now - (chart.lastReadout || 0) >= 200) {
      chart.lastReadout = now;
      state.readoutDirty = false;
      renderReadout();
    }
    stepMomentum(now);
    const interval = now < (chart.interactUntil || 0) ? 0 : 200;
    if (chart.dirty && !document.hidden && now - (chart.lastDraw || 0) >= interval) {
      chart.lastDraw = now;
      try { drawChart(); } catch (err) { console.error(err); }
      chart.dirty = false;
    }
    requestAnimationFrame(chartTick);
  }

  // ------------------------------------------------------------------
  // 表格
  // ------------------------------------------------------------------
  function renderTable() {
    if (!state.tableOpen) return;
    const unit = baseUnit();
    const wrap = $('tableWrap');
    // 用户向下翻看时不刷新，避免内容跳动
    if (wrap.scrollTop > 4) return;
    const rows = state.samples.slice(-200).reverse().map(s => {
      const v = s.ol || s.d === null ? 'OL' : (() => { const x = fmtEng(s.d, s.u || unit, 6); return x.text + ' ' + x.unit; })();
      const st = s.alarm === 'low' ? '低于下限' : s.alarm === 'high' ? '高于上限' : s.alarm === 'ol' ? '超量程' : '正常';
      const cls = s.alarm === 'low' || s.alarm === 'ol' ? 'st-bad' : s.alarm === 'high' ? 'st-warn' : '';
      return `<tr><td>${timeStr(s.t)}</td><td>${v}</td><td class="${cls}">${st}</td></tr>`;
    });
    $('sampleTable').innerHTML = rows.length ? rows.join('')
      : '<tr><td colspan="3" style="color:var(--muted)">暂无数据</td></tr>';
  }
  setInterval(renderTable, 500);

  function setView(view) {
    state.tableOpen = view === 'table';
    $('tableWrap').classList.toggle('hidden', !state.tableOpen);
    $('chartWrap').classList.toggle('hidden', state.tableOpen);
    for (const b of $('viewGroup').querySelectorAll('button')) {
      b.classList.toggle('active', b.dataset.view === view);
    }
    if (state.tableOpen) renderTable();
    else chart.dirty = true;
    try { localStorage.setItem('dmm-view', view); } catch (_) {}
  }
  $('viewGroup').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button');
    if (btn) setView(btn.dataset.view);
  });

  // ------------------------------------------------------------------
  // 控件事件
  // ------------------------------------------------------------------
  async function applySelect(el, action, label) {
    const requested = el.value;
    const text = el.options[el.selectedIndex] ? el.options[el.selectedIndex].textContent : requested;
    el.disabled = true;
    el.classList.add('pending');
    let ok = false;
    try {
      const result = await postJSON('/api/control', { action, value: requested });
      const applied = result.applied && result.applied[action];
      el.classList.remove('pending');
      if (applied && applied !== requested) {
        showToast(`仪器未接受该${label}，当前为 ${applied}`, 'error');
        renderControls();
      } else {
        ok = true;
        showToast(`${label}：${text}`, 'ok');
      }
    } catch (err) {
      el.classList.remove('pending');
      showToast(err.message, 'error');
      renderControls();
    } finally {
      el.classList.remove('pending');
      el.disabled = false;
      renderDevice();
      if (ok) flashOk(el);
    }
  }

  $('rangeSelect').addEventListener('change', () => applySelect($('rangeSelect'), 'range', '量程'));
  $('rateSelect').addEventListener('change', () => applySelect($('rateSelect'), 'rate', '采样速率'));
  $('triggerSelect').addEventListener('change', () => applySelect($('triggerSelect'), 'trigger', '触发源'));

  $('syncState').addEventListener('click', async () => {
    const btn = $('syncState');
    if (btn.classList.contains('spinning')) return;
    btn.classList.add('spinning');
    try {
      await postJSON('/api/control', { action: 'sync' });
      showToast('已从仪器回读状态', 'ok');
      loadIntegrationTime();
    } catch (err) { showToast(err.message, 'error'); }
    finally { btn.classList.remove('spinning'); }
  });

  $('modeGroup').addEventListener('click', async (ev) => {
    const btn = ev.target.closest('button');
    if (!btn) return;
    const mode = btn.dataset.mode;
    if (mode === state.config.display_mode) return;
    const r = (parseFloat($('shuntValue').value) || 0) * parseFloat($('shuntUnit').value);
    if (!(r > 0)) { markInvalid($('shuntValue'), '请先填写有效的分流电阻'); return; }
    if (mode === 'current' && state.instrument.function && state.instrument.function !== 'DCV') {
      const ok = await confirmDialog(
        `分流电流模式需要使用 DCV 档。\n当前功能「${funcLabel(state.instrument.function)}」将被切换为直流电压。`,
        { title: '切换到分流电流模式', ok: '切换' });
      if (!ok) return;
    }
    const group = $('modeGroup');
    group.style.pointerEvents = 'none';
    try {
      await postJSON('/api/config', { display_mode: mode, shunt_ohm: r });
      state.shuntDirty = false;
      state.limitsMode = null;
      renderLimits(true);
      renderDirty();
      showToast(mode === 'current' ? '已切换到分流电流模式 · ' + fmtR(r) : '已切换到直接电压模式', 'ok');
    } catch (err) { showToast(err.message, 'error'); }
    finally { group.style.pointerEvents = ''; }
  });

  $('shuntValue').addEventListener('input', () => { state.shuntDirty = true; renderDirty(); });
  $('shuntUnit').addEventListener('change', () => { state.shuntDirty = true; renderDirty(); });
  onEnter($('shuntValue'), () => $('applyShunt').click());

  $('applyShunt').addEventListener('click', () => {
    const r = (parseFloat($('shuntValue').value) || 0) * parseFloat($('shuntUnit').value);
    if (!(r > 0)) { markInvalid($('shuntValue'), '分流电阻必须大于 0'); return; }
    return withBusy($('applyShunt'), async () => {
      try {
        await postJSON('/api/config', { display_mode: state.config.display_mode, shunt_ohm: r });
        state.shuntDirty = false;
        renderDirty();
        flashOk($('shuntValue'));
        showToast('分流设置已应用 · ' + fmtR(r), 'ok');
      } catch (err) { showToast(err.message, 'error'); }
    });
  });

  const markLimitsDirty = () => { state.limitsDirty = true; renderDirty(); };
  for (const id of ['limitLow', 'limitHigh']) {
    $(id).addEventListener('input', markLimitsDirty);
    onEnter($(id), () => $('applyLimits').click());
  }
  $('limitLowUnit').addEventListener('change', markLimitsDirty);
  $('limitHighUnit').addEventListener('change', markLimitsDirty);
  $('limitEnabled').addEventListener('change', markLimitsDirty);

  $('applyLimits').addEventListener('click', () => {
    const mode = limitKey();
    if (!mode) { showToast('当前功能不支持限值报警', 'error'); return; }
    const lowRaw = $('limitLow').value;
    const highRaw = $('limitHigh').value;
    const low = lowRaw === '' ? null : parseFloat(lowRaw) * parseFloat($('limitLowUnit').value);
    const high = highRaw === '' ? null : parseFloat(highRaw) * parseFloat($('limitHighUnit').value);
    if (lowRaw !== '' && !Number.isFinite(low)) { markInvalid($('limitLow'), '下限不是有效数字'); return; }
    if (highRaw !== '' && !Number.isFinite(high)) { markInvalid($('limitHigh'), '上限不是有效数字'); return; }
    if ($('limitEnabled').checked && (low === null || high === null)) {
      markInvalid(low === null ? $('limitLow') : $('limitHigh'), '启用报警时请同时填写上下限');
      return;
    }
    if (low !== null && high !== null && low >= high) {
      markInvalid($('limitHigh'), '下限必须小于上限');
      return;
    }
    return withBusy($('applyLimits'), async () => {
      try {
        await postJSON('/api/limits', {
          mode, enabled: $('limitEnabled').checked, low, high,
        });
        state.limitsDirty = false;
        renderDirty();
        flashOk($('limitLow'), $('limitHigh'));
        showToast($('limitEnabled').checked ? '限值已应用' : '限值已保存（报警未启用）', 'ok');
      } catch (err) { showToast(err.message, 'error'); }
    });
  });

  function resetStats() {
    return withBusy($('statsReset'), async () => {
      try { await postJSON('/api/stats/reset', {}); showToast('统计已重置', 'ok'); }
      catch (err) { showToast(err.message, 'error'); }
    });
  }
  $('statsReset').addEventListener('click', resetStats);

  function setStatsConfigOpen(open) {
    const panel = $('statsConfig');
    panel.classList.toggle('hidden', !open);
    $('statsConfigBtn').setAttribute('aria-expanded', open ? 'true' : 'false');
    $('statsConfigBtn').classList.toggle('active', open);
    if (open) renderStatsConfig();
  }
  $('statsConfigBtn').addEventListener('click', (ev) => {
    ev.stopPropagation();
    setStatsConfigOpen($('statsConfig').classList.contains('hidden'));
  });
  $('statsConfigClose').addEventListener('click', () => setStatsConfigOpen(false));
  // 点击弹层外部关闭
  document.addEventListener('pointerdown', (ev) => {
    const panel = $('statsConfig');
    if (panel.classList.contains('hidden')) return;
    if (panel.contains(ev.target) || $('statsConfigBtn').contains(ev.target)) return;
    setStatsConfigOpen(false);
  });
  $('statsConfigDefault').addEventListener('click', () => {
    state.statMetrics = [...DEFAULT_STAT_METRICS];
    saveStatMetrics();
    renderStatsConfig();
    renderStats();
  });
  $('statsConfigAll').addEventListener('click', () => {
    state.statMetrics = STAT_DEFS.map(d => d.key);
    saveStatMetrics();
    renderStatsConfig();
    renderStats();
  });

  $('logStart').addEventListener('click', () => withBusy($('logStart'), async () => {
    try {
      const d = await postJSON('/api/log/start', {});
      state.logging = d.logging; renderLogging(); fetchLogs();
      showToast('开始记录 · ' + (d.logging && d.logging.file ? d.logging.file : 'CSV'), 'ok');
    } catch (err) { showToast(err.message, 'error'); }
  }));
  $('logStop').addEventListener('click', () => withBusy($('logStop'), async () => {
    try {
      const d = await postJSON('/api/log/stop', {});
      const lg = d.logging || {};
      state.logging = lg; renderLogging(); fetchLogs();
      showToast(`记录已停止 · 共 ${lg.rows || 0} 行`, 'ok');
    } catch (err) { showToast(err.message, 'error'); }
  }));
  $('refreshLogs').addEventListener('click', () => withBusy($('refreshLogs'), () => fetchLogs(true)));

  function fmtSize(n) {
    if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
    if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
    return n + ' B';
  }

  async function fetchLogs(manual = false) {
    try {
      const d = await api('/api/logs');
      state.logging = d.logging || state.logging;
      renderLogging();
      const list = $('logList');
      const prevKnown = state.knownLogs;
      state.knownLogs = new Set(d.files.map(f => f.name));
      list.innerHTML = '';
      if (!d.files.length) {
        list.innerHTML = '<div class="log-empty">暂无记录文件 · 点击「开始记录」保存 CSV</div>';
        return;
      }
      for (const f of d.files) {
        const div = document.createElement('div');
        const isNew = prevKnown && !prevKnown.has(f.name);
        div.className = 'log-item' + (f.active ? ' active' : '') + (isNew ? ' new' : '');
        const name = document.createElement('span');
        name.className = 'name';
        name.textContent = f.name;
        name.title = f.name;
        const meta = document.createElement('span');
        meta.className = 'meta';
        meta.textContent = fmtSize(f.size) + (f.mtime ? ' · ' + fmtAgo(f.mtime) : '');
        const a = document.createElement('a');
        a.href = `/api/logs/${encodeURIComponent(f.name)}/download`;
        a.textContent = '下载';
        a.setAttribute('download', f.name);
        const an = document.createElement('button');
        an.type = 'button';
        an.className = 'log-analyze';
        an.textContent = '分析';
        an.title = '分析 / 回放这个记录';
        an.addEventListener('click', () => openReplay(f.name));
        const acts = document.createElement('span');
        acts.className = 'log-actions';
        acts.append(an, a);
        div.append(name, meta, acts);
        list.appendChild(div);
      }
      if (manual) showToast(`共 ${d.files.length} 个记录文件`);
    } catch (err) { showToast(err.message, 'error'); }
  }

  // 窗口切换
  $('windowGroup').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button');
    if (!btn) return;
    state.windowSec = parseInt(btn.dataset.window, 10);
    try { localStorage.setItem('dmm-window', String(state.windowSec)); } catch (_) {}
    if (state.tableOpen) setView('chart');
    state.view = { live: true, start: 0, end: 0 };
    setZoomUI();
    chart.dirty = true;
    if (state.windowSec === 0 || state.windowSec > 60) fetchHistory();
  });

  $('zoomIn').addEventListener('click', () => zoomBy(1 / 1.6));
  $('zoomOut').addEventListener('click', () => zoomBy(1.6));
  $('zoomLive').addEventListener('click', resetZoomView);

  // ------------------------------------------------------------------
  // 高级设置抽屉
  // ------------------------------------------------------------------
  const adv = { schema: null, fn: null };

  function advIsOpen() {
    const d = $('advDrawer');
    return !d.classList.contains('hidden') && !d.classList.contains('closing');
  }

  function advOpen() {
    const d = $('advDrawer'), b = $('advBackdrop');
    clearTimeout(adv.closeTimer);
    d.classList.remove('closing'); b.classList.remove('closing');
    b.classList.remove('hidden');
    d.classList.remove('hidden');
    d.setAttribute('aria-hidden', 'false');
    adv.prevFocus = document.activeElement;
    setTimeout(() => $('advClose').focus(), 30);
    loadAdvanced();
  }

  function advClose() {
    const d = $('advDrawer'), b = $('advBackdrop');
    if (d.classList.contains('hidden')) return;
    d.setAttribute('aria-hidden', 'true');
    const finish = () => {
      d.classList.add('hidden'); b.classList.add('hidden');
      d.classList.remove('closing'); b.classList.remove('closing');
    };
    if (REDUCED_MOTION) { finish(); }
    else {
      d.classList.add('closing'); b.classList.add('closing');
      adv.closeTimer = setTimeout(finish, 200);
    }
    if (adv.prevFocus && adv.prevFocus.focus) adv.prevFocus.focus();
  }

  function filterAdvanced() {
    const q = ($('advSearch').value || '').trim().toLowerCase();
    for (const g of $('advBody').querySelectorAll('.adv-group')) {
      let shown = 0;
      for (const it of g.querySelectorAll('.adv-item')) {
        const hit = !q || (it.dataset.search || '').includes(q);
        it.classList.toggle('hidden', !hit);
        if (hit) shown++;
      }
      const title = (g.querySelector('.adv-group-title') || {}).textContent || '';
      const groupHit = q && title.toLowerCase().includes(q);
      if (groupHit) for (const it of g.querySelectorAll('.adv-item')) it.classList.remove('hidden');
      g.classList.toggle('hidden', !!q && !shown && !groupHit);
      if (q && (shown || groupHit) && !g.open) g.open = true;
    }
  }
  $('advSearch').addEventListener('input', filterAdvanced);
  $('advSearch').addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && $('advSearch').value) {
      e.stopPropagation();
      $('advSearch').value = '';
      filterAdvanced();
    }
  });

  async function loadAdvanced() {
    if (!adv.schema) {
      try {
        const d = await api('/api/advanced');
        adv.schema = d.groups || [];
      } catch (err) {
        showToast('加载高级设置失败：' + err.message, 'error');
        return;
      }
    }
    renderAdvanced();
  }

  function activeFunction() {
    return state.instrument.function || 'DCV';
  }

  function findAdvItem(key) {
    if (!adv.schema) return null;
    for (const g of adv.schema) {
      for (const it of g.items) {
        if (it.key === key) return it;
      }
    }
    return null;
  }

  function renderAdvanced() {
    if (!adv.schema) return;
    const body = $('advBody');
    body.innerHTML = '';
    const fn = activeFunction();
    adv.fn = fn;
    $('advSubtitle').textContent = `当前功能 ${funcLabel(fn)} (${fn}) · ADC 命令语言`;

    for (const group of adv.schema) {
      const items = group.items.filter(it => !it.applies || it.applies.includes(fn));
      const details = document.createElement('details');
      details.className = 'adv-group';
      details.dataset.group = group.key;
      const summary = document.createElement('summary');
      summary.innerHTML = `<span class="adv-group-title">${group.label}</span>`
        + `<span class="adv-group-count">${items.length} 项</span>`;
      details.appendChild(summary);

      const wrap = document.createElement('div');
      wrap.className = 'adv-items';
      if (!items.length) {
        wrap.innerHTML = '<div class="adv-note">当前功能不适用此组设置。</div>';
      }
      for (const item of items) wrap.appendChild(renderAdvItem(item));
      details.appendChild(wrap);
      details.addEventListener('toggle', () => {
        if (details.open) loadAdvGroup(details);
      });
      body.appendChild(details);
    }

    const first = body.querySelector('.adv-group');
    if (first) {
      first.open = true;
      loadAdvGroup(first);
    }
    filterAdvanced();
  }

  function renderAdvItem(item) {
    const div = document.createElement('div');
    div.className = 'adv-item';
    div.dataset.key = item.key;
    div.dataset.search = [item.label, item.key, item.hint || '', item.command || '']
      .join(' ').toLowerCase();

    const label = document.createElement('span');
    label.className = 'adv-label';
    label.textContent = item.label;

    const readback = document.createElement('span');
    readback.className = 'adv-readback';
    readback.id = 'adv-val-' + item.key;
    readback.textContent = '—';

    div.appendChild(label);
    div.appendChild(readback);

    const row = document.createElement('div');
    row.className = 'adv-row';

    if (item.kind === 'readout') {
      const btn = document.createElement('button');
      btn.className = 'btn btn-ghost btn-sm';
      btn.textContent = '读取';
      btn.addEventListener('click', () => loadAdvKeys([item.key]));
      row.appendChild(btn);
    } else if (item.kind === 'action') {
      const btn = document.createElement('button');
      btn.className = 'btn btn-primary btn-sm';
      btn.textContent = item.action_label || '执行';
      btn.addEventListener('click', () => runAdvAction(item, btn));
      row.appendChild(btn);
    } else if (item.kind === 'select') {
      const sel = document.createElement('select');
      sel.dataset.advSelect = item.key;
      for (const [code, text] of (item.options || [])) {
        const o = document.createElement('option');
        o.value = code;
        o.textContent = text;
        sel.appendChild(o);
      }
      sel.addEventListener('change', () => {
        sel.dataset.userEdited = '1';
        setAdvValue(item, sel.value, readback);
      });
      row.appendChild(sel);
    } else if (item.kind === 'number') {
      const input = document.createElement('input');
      input.type = 'number';
      if (item.min !== undefined) input.min = item.min;
      if (item.max !== undefined) input.max = item.max;
      if (item.step !== undefined) input.step = item.step;
      input.id = 'adv-input-' + item.key;
      input.placeholder = item.hint || '';
      input.addEventListener('input', () => { input.dataset.userEdited = '1'; });
      row.appendChild(input);
      if (item.unit) {
        const u = document.createElement('span');
        u.className = 'unit';
        u.textContent = item.unit;
        row.appendChild(u);
      }
      const btn = document.createElement('button');
      btn.className = 'btn btn-ghost btn-sm';
      btn.textContent = '应用';
      btn.addEventListener('click', () => {
        if (input.value === '') { markInvalid(input, '请输入数值'); return; }
        withBusy(btn, () => setAdvValue(item, input.value, readback));
      });
      onEnter(input, () => btn.click());
      row.appendChild(btn);
      if (item.action) {
        const act = document.createElement('button');
        act.className = 'btn btn-ghost btn-sm';
        act.textContent = item.action_label || '取当前值';
        act.addEventListener('click', () => runAdvAction(item, act));
        row.appendChild(act);
      }
    }

    div.appendChild(row);
    if (item.hint) {
      const hint = document.createElement('div');
      hint.className = 'adv-hint';
      hint.textContent = item.hint;
      div.appendChild(hint);
    }
    return div;
  }

  function advGroupKeys(details) {
    const keys = [];
    for (const div of details.querySelectorAll('.adv-item')) {
      const item = findAdvItem(div.dataset.key);
      // 纯读数项由「读取」按钮按需读取，避免打开分组时等待过久
      if (item && item.query && item.kind !== 'readout') keys.push(item.key);
    }
    return keys;
  }

  async function loadAdvGroup(details) {
    const keys = advGroupKeys(details);
    if (!keys.length) return;
    details.dataset.loading = '1';
    try {
      await loadAdvKeys(keys);
    } finally {
      delete details.dataset.loading;
    }
  }

  async function loadAdvKeys(keys) {
    if (!keys.length) return;
    try {
      const d = await api('/api/advanced/read?keys=' + encodeURIComponent(keys.join(',')));
      const values = d.values || {};
      for (const [key, res] of Object.entries(values)) {
        const item = findAdvItem(key);
        const el = $('adv-val-' + key);
        if (!item || !el) continue;
        if (!res.ok) {
          el.textContent = res.error || '读取失败';
          el.className = 'adv-readback err';
          continue;
        }
        if (el.textContent !== String(res.value)) {
          el.textContent = res.value;
          el.className = 'adv-readback ok';
          restartClass(el, 'flash');
        } else {
          el.className = 'adv-readback ok';
        }
        if (item.kind === 'select') {
          const sel = document.querySelector(`select[data-adv-select="${key}"]`);
          const matched = (item.options || []).find(([code]) =>
            String(res.value).toUpperCase().startsWith(code));
          if (sel && matched && sel.dataset.userEdited !== '1') sel.value = matched[0];
        } else if (item.kind === 'number') {
          const input = $('adv-input-' + key);
          const num = parseAdvNumber(res.value);
          if (input && num !== null && input.dataset.userEdited !== '1') input.value = num;
        }
      }
    } catch (err) {
      showToast('读取高级设置失败：' + err.message, 'error');
    }
  }

  function parseAdvNumber(reply) {
    const m = String(reply).match(/[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?/);
    return m ? Number(m[0]) : null;
  }

  async function setAdvValue(item, value, readback) {
    if ((state.device.state || 'offline') !== 'online') {
      showToast('设备未连接，无法下发设置', 'error');
      return;
    }
    if (value === '' || value === null || value === undefined) {
      showToast('请输入数值', 'error');
      return;
    }
    try {
      const d = await postJSON('/api/advanced/set', { key: item.key, value });
      if (readback) {
        readback.textContent = d.value || '已应用';
        readback.className = 'adv-readback ok';
        restartClass(readback, 'flash');
      }
      const inp = $('adv-input-' + item.key);
      if (inp) delete inp.dataset.userEdited;
      showToast(item.label + ' 已设置', 'ok');
      if (item.key === 'itp' || item.key === 'its' || item.key === 'digits') {
        loadIntegrationTime();
      }
      if (item.key === 'digits') renderReadout();
    } catch (err) {
      showToast(err.message, 'error');
      if (readback) {
        readback.textContent = err.message;
        readback.className = 'adv-readback err';
      }
    }
  }

  async function runAdvAction(item, btn) {
    if ((state.device.state || 'offline') !== 'online') {
      showToast('设备未连接，无法下发设置', 'error');
      return;
    }
    if (item.confirm && !(await confirmDialog(item.confirm, { title: item.label }))) return;
    const old = btn.textContent;
    btn.disabled = true;
    btn.classList.add('is-busy');
    try {
      const d = await postJSON('/api/advanced/action', { key: item.key });
      const el = $('adv-val-' + item.key);
      if (el) {
        el.textContent = d.value || '已执行';
        el.className = 'adv-readback ok';
      }
      if (item.action && d.value) {
        const input = $('adv-input-' + item.key);
        const num = parseAdvNumber(d.value);
        if (input && num !== null) input.value = num;
      }
      showToast(item.label + ' 已执行', 'ok');
    } catch (err) {
      showToast(err.message, 'error');
    } finally {
      btn.disabled = false;
      btn.classList.remove('is-busy');
      btn.textContent = old;
    }
  }

  $('advOpen').addEventListener('click', advOpen);
  $('advClose').addEventListener('click', advClose);
  $('advBackdrop').addEventListener('click', advClose);
  $('advRefresh').addEventListener('click', async () => {
    const open = [...$('advBody').querySelectorAll('.adv-group[open]')];
    if (!open.length) {
      showToast('请先展开要刷新的分组');
      return;
    }
    await withBusy($('advRefresh'), () => Promise.all(open.map(g => loadAdvGroup(g))));
    showToast('高级设置已刷新', 'ok');
  });

  // ------------------------------------------------------------------
  // HOLD / 复制 / 快捷键
  // ------------------------------------------------------------------
  function setHold(on, silent = false) {
    if (on && !state.latest) {
      if (!silent) showToast('暂无读数可保持', 'error');
      return;
    }
    state.hold = on ? state.latest : null;
    const btn = $('holdToggle');
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.querySelector('span').textContent = on ? '释放 HOLD' : '保持 HOLD';
    renderReadout();
    if (!silent) showToast(on ? '读数已保持（采集与记录照常进行）' : '已释放保持');
  }
  $('holdToggle').addEventListener('click', () => setHold(!state.hold));

  async function copyReading() {
    const s = state.hold || state.latest;
    const txt = readoutText(s);
    if (!txt) { showToast('暂无读数可复制', 'error'); return; }
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(txt);
      } else {
        const ta = document.createElement('textarea');
        ta.value = txt;
        ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      restartClass($('valueCopy'), 'copied');
      showToast('已复制 ' + txt, 'ok');
    } catch (_) {
      showToast('复制失败，请手动选择', 'error');
    }
  }
  $('valueCopy').addEventListener('click', copyReading);
  $('copyValue').addEventListener('click', copyReading);

  function setLayer(id, open) {
    $(id).classList.toggle('hidden', !open);
    if (open) {
      const btn = $(id).querySelector('.btn-primary, .btn');
      if (btn) setTimeout(() => btn.focus(), 30);
    }
  }
  $('helpOpen').addEventListener('click', () => setLayer('helpLayer', true));
  $('helpClose').addEventListener('click', () => setLayer('helpLayer', false));
  $('helpLayer').addEventListener('click', (e) => { if (e.target === $('helpLayer')) setLayer('helpLayer', false); });

  function isTyping(el) {
    if (!el) return false;
    const tag = el.tagName;
    return tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' || el.isContentEditable;
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (!$('helpLayer').classList.contains('hidden')) { setLayer('helpLayer', false); return; }
      if (!$('statsConfig').classList.contains('hidden')) { setStatsConfigOpen(false); return; }
      if (advIsOpen()) { advClose(); return; }
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey || isTyping(e.target)) return;
    if (!$('confirmLayer').classList.contains('hidden')) return;
    const k = e.key;
    if (k === '?' ) { setLayer('helpLayer', $('helpLayer').classList.contains('hidden')); e.preventDefault(); return; }
    if (advIsOpen() || !$('helpLayer').classList.contains('hidden')) return;
    switch (k.toLowerCase()) {
      case 'h': setHold(!state.hold); break;
      case 'c': copyReading(); break;
      case 'l': setView('chart'); resetZoomView(); break;
      case 't': setView(state.tableOpen ? 'chart' : 'table'); break;
      case 'r': resetStats(); break;
      case 'a': advOpen(); break;
      case 'p': toggleRelease(); break;
      case '+': case '=': zoomBy(1 / 1.6); break;
      case '-': case '_': zoomBy(1.6); break;
      default: return;
    }
    e.preventDefault();
  });

  // ------------------------------------------------------------------
  // 记录分析 / 回放
  // ------------------------------------------------------------------
  const rp = {
    open: false, name: null, unit: null, meta: null,
    full: null,        // 整个文件的概览数据（导航条用）
    data: null,        // 当前视图附近的细节数据
    view: { start: 0, end: 1 },
    sel: null,         // { start, end }
    cursor: 0, playing: false, speed: 1, lastFrame: 0,
    mode: 'pan', drag: null, hover: null,
    dirty: true, detailSeq: 0, statsSeq: 0, detailTimer: 0, statsTimer: 0,
    loading: 0, colors: null, colorsKey: '', raf: 0,
  };

  // 横轴范围：当前单位数据的起止；时间标注统一以文件开头为原点
  function rpExtent() {
    const r = rp.full && rp.full.range;
    if (r && r.start !== null && r.start !== undefined) return { start: r.start, end: Math.max(r.end, r.start + 1e-3) };
    const m = rp.meta;
    return m && m.start !== null ? { start: m.start, end: Math.max(m.end, m.start + 1e-3) } : { start: 0, end: 1 };
  }
  function rpOrigin() {
    return rp.meta && rp.meta.start !== null ? rp.meta.start : rpExtent().start;
  }

  function fmtElapsed(sec, step = 1) {
    const neg = sec < 0;
    sec = Math.abs(sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const dec = step < 0.1 ? 2 : step < 1 ? 1 : 0;
    const ss = dec ? s.toFixed(dec).padStart(3 + dec, '0') : String(Math.floor(s)).padStart(2, '0');
    return (neg ? '-' : '') + (h ? h + ':' + String(m).padStart(2, '0') : String(m).padStart(2, '0')) + ':' + ss;
  }

  function fmtDur(sec) {
    if (!Number.isFinite(sec)) return '—';
    if (sec < 1) return (sec * 1000).toFixed(0) + ' ms';
    if (sec < 60) return sec.toFixed(sec < 10 ? 2 : 1) + ' s';
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
    return h ? `${h} h ${m} min` : `${m} min ${s} s`;
  }

  function fmtWall(t) {
    const d = new Date(t * 1000);
    return d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' }) + ' '
      + d.toLocaleTimeString('zh-CN', { hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0');
  }

  function rpColors() {
    const key = state.theme;
    if (rp.colors && rp.colorsKey === key) return rp.colors;
    rp.colorsKey = key;
    rp.colors = {
      ink: cssVar('--ink-2'), muted: cssVar('--muted'), grid: cssVar('--grid'), axis: cssVar('--axis'),
      accent: cssVar('--accent'), bad: cssVar('--bad'), warn: cssVar('--warn'), panel: cssVar('--panel-2'),
    };
    return rp.colors;
  }

  function rpSetLoading(delta) {
    rp.loading = Math.max(0, rp.loading + delta);
    $('rpLoading').classList.toggle('hidden', rp.loading === 0);
  }

  async function rpFetch(params) {
    const q = new URLSearchParams();
    if (rp.unit) q.set('unit', rp.unit);
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null) q.set(k, v);
    return api(`/api/logs/${encodeURIComponent(rp.name)}/analysis?` + q.toString());
  }

  async function openReplay(name) {
    rp.name = name;
    rp.unit = null;
    rp.sel = null;
    rp.playing = false;
    rp.open = true;
    rp.full = rp.data = rp.meta = null;
    setText('replayTitle', name);
    setText('replayMeta', '读取中…');
    $('rpDownload').href = `/api/logs/${encodeURIComponent(name)}/download`;
    $('rpDownload').setAttribute('download', name);
    $('replayLayer').classList.remove('hidden');
    rpRenderPlay();
    rpRenderStats(null);
    if (!rp.raf) rp.raf = requestAnimationFrame(rpTick);
    await rpLoadFull(true);
    setTimeout(() => $('rpChart').focus(), 50);
  }

  function closeReplay() {
    rp.open = false;
    rp.playing = false;
    $('replayLayer').classList.add('hidden');
    $('rpTip').classList.add('hidden');
  }

  async function rpLoadFull(resetView) {
    rpSetLoading(1);
    try {
      const d = await rpFetch({ max_points: 1600 });
      rp.full = d;
      rp.meta = d.meta;
      rp.unit = d.unit;
      rp.active = d.active;
      rpRenderMeta();
      rpRenderUnits();
      const ext = rpExtent();
      if (resetView || !rp.data) {
        rp.view = { ...ext };
        rp.cursor = (d.range && d.range.start) || ext.start;
        rp.data = d;
      } else {
        rpScheduleDetail(0);
      }
      rpRenderStats(d.stats, rp.sel ? null : '全部数据');
      if (rp.sel) rpScheduleStats(0);
      rp.dirty = true;
    } catch (err) {
      setText('replayMeta', '读取失败：' + err.message);
      showToast(err.message, 'error');
    } finally {
      rpSetLoading(-1);
    }
  }

  function rpRenderMeta() {
    const m = rp.meta;
    if (!m || !m.rows) { setText('replayMeta', '文件中没有数据'); return; }
    const rate = m.rate_hz ? ` · 平均 ${m.rate_hz >= 10 ? m.rate_hz.toFixed(1) : m.rate_hz.toFixed(2)} S/s` : '';
    const fns = (rp.full && rp.full.functions || []).map(funcLabel).join(' / ');
    setText('replayMeta', `${fmtWall(m.start).slice(0, -4)} 起 · 时长 ${fmtDur(m.duration)} · ${m.rows.toLocaleString()} 行${rate}`
      + (fns ? ' · ' + fns : '') + (rp.active ? ' · 记录中' : ''));
  }

  function rpRenderUnits() {
    const box = $('rpUnits');
    const units = (rp.meta && rp.meta.units) || [];
    box.classList.toggle('hidden', units.length < 2);
    box.innerHTML = '';
    for (const u of units) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = `${u.unit || '—'} · ${u.rows.toLocaleString()} 行`;
      b.classList.toggle('active', u.unit === rp.unit);
      b.addEventListener('click', () => {
        if (u.unit === rp.unit) return;
        rp.unit = u.unit;
        rp.sel = null;
        rp.playing = false;
        rpRenderPlay();
        rpLoadFull(true);
      });
      box.appendChild(b);
    }
  }

  // 视图变化后按需加载更细的数据（带前后余量，播放时不必频繁请求）
  function rpScheduleDetail(delay = 120) {
    clearTimeout(rp.detailTimer);
    rp.detailTimer = setTimeout(rpLoadDetail, delay);
  }

  function rpDetailOk() {
    const d = rp.data;
    if (!d || !d.range || d.range.start === null) return false;
    const { start, end } = rp.view;
    const ext = rpExtent();
    const covers = d.range.start <= Math.max(start, ext.start) + 1e-9 && d.range.end >= Math.min(end, ext.end) - 1e-9;
    if (!covers) return false;
    if (d.raw) return true;
    const span = d.range.end - d.range.start;
    const width = $('rpChart').clientWidth || 800;
    return (end - start) / span * d.points.length >= width * 0.9;
  }

  async function rpLoadDetail() {
    if (!rp.open || !rp.name || rpDetailOk()) return;
    const ext = rpExtent();
    const span = rp.view.end - rp.view.start;
    const pad = rp.playing ? span * 1.5 : span * 0.5;
    const start = Math.max(ext.start, rp.view.start - pad);
    const end = Math.min(ext.end, rp.view.end + pad);
    const width = $('rpChart').clientWidth || 800;
    const maxPoints = Math.min(20000, Math.ceil(width * 1.5 * (end - start) / span));
    const seq = ++rp.detailSeq;
    rpSetLoading(1);
    try {
      const d = await rpFetch({ start, end, max_points: maxPoints });
      if (seq !== rp.detailSeq) return;
      d.range = { start, end, rows: d.range.rows };
      rp.data = d;
      rp.dirty = true;
    } catch (err) {
      if (seq === rp.detailSeq) showToast(err.message, 'error');
    } finally {
      rpSetLoading(-1);
    }
  }

  function rpStatsRange() {
    if (rp.sel) return { ...rp.sel, title: '选区' };
    const ext = rpExtent();
    const full = rp.view.start <= ext.start + 1e-9 && rp.view.end >= ext.end - 1e-9;
    return full ? null : { start: rp.view.start, end: rp.view.end, title: '可见范围' };
  }

  function rpScheduleStats(delay = 250) {
    clearTimeout(rp.statsTimer);
    rp.statsTimer = setTimeout(rpLoadStats, delay);
  }

  async function rpLoadStats() {
    if (!rp.open) return;
    const r = rpStatsRange();
    if (!r) { rpRenderStats(rp.full && rp.full.stats, '全部数据'); return; }
    const seq = ++rp.statsSeq;
    $('rpStats').classList.add('loading');
    try {
      const d = await rpFetch({ start: r.start, end: r.end, max_points: 10 });
      if (seq !== rp.statsSeq) return;
      rpRenderStats(d.stats, `${r.title} ${fmtElapsed(r.start - rpOrigin())} – ${fmtElapsed(r.end - rpOrigin())}`);
    } catch (_) { /* 保留上一次 */ } finally {
      if (seq === rp.statsSeq) $('rpStats').classList.remove('loading');
    }
  }

  function rpRenderStats(st, title) {
    if (title) setText('rpStatsTitle', title);
    const u = rp.unit || '';
    const f = (v) => {
      if (v === null || v === undefined || !Number.isFinite(v)) return '—';
      const x = fmtEng(v, u, 6);
      return x.text + ' ' + x.unit;
    };
    const items = st ? [
      ['平均值', f(st.mean)], ['标准差', f(st.std)],
      ['最小值', f(st.min)], ['最大值', f(st.max)],
      ['峰峰值', f(st.pp)], ['RMS', f(st.rms)],
      ['中位数', f(st.median)], ['时长', fmtDur(st.duration)],
      ['有效样本', (st.n || 0).toLocaleString()],
      ['OL', (st.n_ol || 0).toLocaleString(), st.n_ol > 0],
      ['超限', (st.n_alarm || 0).toLocaleString(), st.n_alarm > 0],
      ['采样率', st.duration > 0 ? ((st.n + st.n_ol - 1) / st.duration).toFixed(2) + ' S/s' : '—'],
    ] : [];
    const box = $('rpStats');
    box.innerHTML = '';
    for (const [label, value, warn] of items) {
      const d = document.createElement('div');
      d.className = 'rp-stat' + (warn ? ' warn' : '');
      d.innerHTML = '<label></label><b></b>';
      d.firstChild.textContent = label;
      d.lastChild.textContent = value;
      d.lastChild.title = value;
      box.appendChild(d);
    }
  }

  // ---- 视图操作 -----------------------------------------------------------
  function rpMinSpan() {
    const m = rp.meta;
    const dt = m && m.rate_hz ? 1 / m.rate_hz : 0.05;
    return Math.max(dt * 8, 0.05);
  }

  function rpSetView(start, end, { stats = true } = {}) {
    const ext = rpExtent();
    const full = ext.end - ext.start;
    let span = Math.min(Math.max(end - start, rpMinSpan()), full);
    if (start < ext.start) start = ext.start;
    if (start + span > ext.end) start = ext.end - span;
    rp.view = { start, end: start + span };
    rp.dirty = true;
    rpScheduleDetail(rp.playing ? 0 : 120);
    if (stats && !rp.sel && !rp.playing) rpScheduleStats();
  }

  function rpZoom(factor, anchorT) {
    const { start, end } = rp.view;
    const t = anchorT ?? (start + end) / 2;
    rpSetView(t - (t - start) * factor, t + (end - t) * factor);
  }

  function rpFit() { const e = rpExtent(); rpSetView(e.start, e.end); }

  function rpSetSel(sel) {
    rp.sel = sel && sel.end - sel.start > 1e-6 ? sel : null;
    $('rpClearSel').disabled = !rp.sel;
    $('rpZoomSel').disabled = !rp.sel;
    rp.dirty = true;
    rpScheduleStats(0);
  }

  function rpSeek(t, { follow = true } = {}) {
    const ext = rpExtent();
    rp.cursor = Math.min(ext.end, Math.max(ext.start, t));
    if (follow) {
      const span = rp.view.end - rp.view.start;
      if (rp.cursor < rp.view.start || rp.cursor > rp.view.end) rpSetView(rp.cursor - span / 2, rp.cursor + span / 2);
    }
    rp.dirty = true;
  }

  function rpRenderPlay() {
    $('rpPlay').classList.toggle('playing', rp.playing);
    setText('rpPlayText', rp.playing ? '暂停' : '播放');
    setText('rpSpeedAnn', rp.speed + '×');
    for (const b of $('rpSpeed').querySelectorAll('button')) b.classList.toggle('active', Number(b.dataset.speed) === rp.speed);
  }

  function rpTogglePlay() {
    if (!rp.meta || !rp.meta.rows) return;
    const ext = rpExtent();
    if (!rp.playing && rp.cursor >= ext.end - 1e-6) rp.cursor = ext.start;
    rp.playing = !rp.playing;
    rp.lastFrame = performance.now();
    rpRenderPlay();
    if (!rp.playing) rpScheduleStats(0);
  }

  // ---- 取值 --------------------------------------------------------------
  function rpPointsFor() {
    const d = rp.data;
    if (d && d.range && d.range.start <= rp.view.start && d.range.end >= rp.view.end) return d;
    return rp.full || d;
  }

  function rpIndexAt(points, t) {
    let lo = 0, hi = points.length - 1;
    if (hi < 0) return -1;
    if (t <= points[0][0]) return 0;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (points[mid][0] <= t) lo = mid; else hi = mid - 1;
    }
    return lo;
  }

  function rpRenderReadout() {
    const ext = rpExtent();
    const d = rp.data && rp.data.range && rp.cursor >= rp.data.range.start && rp.cursor <= rp.data.range.end ? rp.data : rp.full;
    const disp = $('rpDisplay');
    let p = null;
    if (d && d.points.length) p = d.points[rpIndexAt(d.points, rp.cursor)];
    const unit = rp.unit || '';
    const fn = rp.full && rp.full.functions && rp.full.functions.length === 1 ? rp.full.functions[0] : (rp.full && rp.full.functions || []).join('/');
    setText('rpFn', fn || '—');
    const ol = !!(p && (p[3] === null || p[3] === undefined));
    $('rpOl').classList.toggle('on', ol || !!(p && p[4]));
    $('rpAlm').classList.toggle('on', !!(p && p[5]));
    $('rpApprox').classList.toggle('on', !!(d && !d.raw));
    disp.classList.toggle('is-ol', ol);
    disp.classList.toggle('is-alarm', !!(p && p[5]));
    if (!p) { setText('rpValue', '--'); setText('rpUnit', unit); }
    else if (ol) { setText('rpValue', 'OL'); setText('rpUnit', unit); }
    else {
      const x = fmtEng(p[3], unit, 7);
      setText('rpValue', x.text);
      setText('rpUnit', x.unit);
    }
    const origin = rpOrigin();
    setText('rpTime', rp.meta && rp.meta.rows
      ? `${fmtWall(rp.cursor)} · +${fmtElapsed(rp.cursor - origin, 0.01)}` : '—');
    const total = ext.end - ext.start;
    setText('rpClock', `${fmtElapsed(rp.cursor - origin)} / ${fmtElapsed(ext.end - origin)}`);
    if (document.activeElement !== $('rpScrub')) {
      $('rpScrub').value = String(Math.round((rp.cursor - ext.start) / (total || 1) * 10000));
    }
  }

  // ---- 绘制 --------------------------------------------------------------
  function rpCanvas(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  function niceNum(x) {
    const e = Math.floor(Math.log10(x));
    const f = x / Math.pow(10, e);
    return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * Math.pow(10, e);
  }

  const TIME_STEPS = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];

  function rpYRange(points, start, end) {
    let lo = Infinity, hi = -Infinity;
    for (const p of points) {
      if (p[0] < start || p[0] > end || p[1] === null) continue;
      if (p[1] < lo) lo = p[1];
      if (p[2] > hi) hi = p[2];
    }
    if (!Number.isFinite(lo)) return [-1, 1];
    if (hi - lo < Math.abs(hi) * 1e-9 + 1e-15) { const m = Math.abs(hi) * 0.001 || 1e-6; lo -= m; hi += m; }
    const pad = (hi - lo) * 0.08;
    return [lo - pad, hi + pad];
  }

  function rpDrawSeries(ctx, points, x, y, c, { envelope = true, lineWidth = 1.3 } = {}) {
    if (!points.length) return;
    const bucketed = points.some(p => p[1] !== p[2]);
    if (bucketed && envelope) {
      ctx.fillStyle = withAlpha(c.accent, 0.22);
      ctx.beginPath();
      let open = false;
      const flush = (from, to) => {
        // 多边形：上沿 max 向右，下沿 min 向左
        ctx.moveTo(x(points[from][0]), y(points[from][2]));
        for (let i = from + 1; i <= to; i++) ctx.lineTo(x(points[i][0]), y(points[i][2]));
        for (let i = to; i >= from; i--) ctx.lineTo(x(points[i][0]), y(points[i][1]));
        ctx.closePath();
      };
      let segStart = -1;
      for (let i = 0; i <= points.length; i++) {
        const ok = i < points.length && points[i][1] !== null;
        if (ok && segStart < 0) segStart = i;
        if (!ok && segStart >= 0) { flush(segStart, i - 1); segStart = -1; open = true; }
      }
      if (open || segStart >= 0) ctx.fill();
    }
    ctx.strokeStyle = c.accent;
    ctx.lineWidth = lineWidth;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    let pen = false;
    for (const p of points) {
      if (p[3] === null || p[3] === undefined) { pen = false; continue; }
      const px = x(p[0]), py = y(p[3]);
      if (pen) ctx.lineTo(px, py); else { ctx.moveTo(px, py); pen = true; }
    }
    ctx.stroke();
  }

  function rpDraw() {
    const c = rpColors();
    const { ctx, w, h } = rpCanvas($('rpChart'));
    ctx.clearRect(0, 0, w, h);
    const padL = 74, padR = 12, padT = 12, padB = 24;
    const pw = Math.max(10, w - padL - padR), ph = Math.max(10, h - padT - padB);
    const { start, end } = rp.view;
    const span = end - start || 1;
    const ext = rpExtent();
    const src = rpPointsFor();
    const pts = src ? src.points : [];
    const i0 = Math.max(0, rpIndexAt(pts, start) - 1);
    const i1 = Math.min(pts.length, rpIndexAt(pts, end) + 2);
    const vis = pts.slice(i0, i1);
    const [ylo, yhi] = rpYRange(vis, start, end);
    const x = (t) => padL + (t - start) / span * pw;
    const y = (v) => padT + (1 - (v - ylo) / (yhi - ylo)) * ph;
    rp.geom = { padL, padR, padT, padB, pw, ph, x, y, ylo, yhi, w, h };

    // 网格与坐标
    ctx.font = '11px ' + cssVar('--mono');
    ctx.textBaseline = 'middle';
    const ystep = niceNum((yhi - ylo) / Math.max(2, Math.floor(ph / 46)));
    ctx.strokeStyle = c.grid; ctx.lineWidth = 1; ctx.fillStyle = c.muted; ctx.textAlign = 'right';
    for (let v = Math.ceil(ylo / ystep) * ystep; v <= yhi; v += ystep) {
      const py = Math.round(y(v)) + 0.5;
      ctx.beginPath(); ctx.moveTo(padL, py); ctx.lineTo(padL + pw, py); ctx.stroke();
      const lab = fmtEng(Math.abs(v) < ystep * 1e-6 ? 0 : v, rp.unit || '', 4);
      ctx.fillText(lab.text + ' ' + lab.unit, padL - 6, py);
    }
    const tstep = TIME_STEPS.find(s => s >= span / Math.max(2, pw / 92)) || 86400;
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    const origin = rpOrigin();
    const rel0 = start - origin;
    for (let r = Math.ceil(rel0 / tstep) * tstep; r <= rel0 + span; r += tstep) {
      const px = Math.round(x(origin + r)) + 0.5;
      ctx.beginPath(); ctx.moveTo(px, padT); ctx.lineTo(px, padT + ph); ctx.stroke();
      ctx.fillText(fmtElapsed(r, tstep), px, padT + ph + 6);
    }

    ctx.save();
    ctx.beginPath(); ctx.rect(padL, padT, pw, ph); ctx.clip();

    // OL / 超限区间：连续的点合并成一段，淡色底 + 顶部（超限）/底部（OL）色带
    const runs = (col) => {
      const out = [];
      let a = null;
      for (let i = 0; i < vis.length; i++) {
        const on = !!vis[i][col];
        if (on && a === null) a = i;
        if ((!on || i === vis.length - 1) && a !== null) {
          const b = on ? i : i - 1;
          const half = vis.length > 1 ? (x(vis[Math.min(vis.length - 1, b + 1)][0]) - x(vis[b][0])) / 2 : 1;
          out.push([x(vis[a][0]) - Math.max(1, half), x(vis[b][0]) + Math.max(1, half)]);
          a = null;
        }
      }
      return out;
    };
    for (const [a, b] of runs(5)) {
      ctx.fillStyle = withAlpha(c.bad, 0.07); ctx.fillRect(a, padT, b - a, ph);
      ctx.fillStyle = withAlpha(c.bad, 0.75); ctx.fillRect(a, padT, Math.max(2, b - a), 3);
    }
    for (const [a, b] of runs(4)) {
      ctx.fillStyle = withAlpha(c.warn, 0.08); ctx.fillRect(a, padT, b - a, ph);
      ctx.fillStyle = withAlpha(c.warn, 0.85); ctx.fillRect(a, padT + ph - 3, Math.max(2, b - a), 3);
    }
    // 功能/单位切换标记
    if (rp.meta && rp.meta.segments) {
      ctx.setLineDash([3, 4]); ctx.strokeStyle = c.warn; ctx.fillStyle = c.warn;
      ctx.textAlign = 'left'; ctx.textBaseline = 'top';
      rp.meta.segments.forEach((sg, i) => {
        if (i === 0 || sg.start < start || sg.start > end) return;
        const px = Math.round(x(sg.start)) + 0.5;
        ctx.beginPath(); ctx.moveTo(px, padT); ctx.lineTo(px, padT + ph); ctx.stroke();
        ctx.fillText(sg.function || sg.unit, px + 4, padT + 2);
      });
      ctx.setLineDash([]);
    }
    // 选区
    if (rp.sel) {
      const sx = x(rp.sel.start), ex = x(rp.sel.end);
      ctx.fillStyle = withAlpha(c.accent, 0.12);
      ctx.fillRect(sx, padT, ex - sx, ph);
      ctx.strokeStyle = withAlpha(c.accent, 0.8); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(sx + 0.5, padT); ctx.lineTo(sx + 0.5, padT + ph);
      ctx.moveTo(ex - 0.5, padT); ctx.lineTo(ex - 0.5, padT + ph); ctx.stroke();
    }
    rpDrawSeries(ctx, vis, x, y, c);

    // 播放光标
    if (rp.cursor >= start && rp.cursor <= end) {
      const cx = Math.round(x(rp.cursor)) + 0.5;
      ctx.strokeStyle = c.warn; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(cx, padT); ctx.lineTo(cx, padT + ph); ctx.stroke();
      const p = vis.length ? vis[rpIndexAt(vis, rp.cursor)] : null;
      if (p && p[3] !== null && p[3] !== undefined) {
        ctx.fillStyle = c.warn;
        ctx.beginPath(); ctx.arc(cx, y(p[3]), 4, 0, Math.PI * 2); ctx.fill();
      }
    }
    // 悬停十字线
    if (rp.hover && !rp.drag) {
      ctx.strokeStyle = withAlpha(c.ink, 0.35); ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(rp.hover.x + 0.5, padT); ctx.lineTo(rp.hover.x + 0.5, padT + ph); ctx.stroke();
      ctx.setLineDash([]);
    }
    // 拖选预览
    if (rp.drag && rp.drag.kind === 'select') {
      const a = Math.min(rp.drag.x0, rp.drag.x1), b = Math.max(rp.drag.x0, rp.drag.x1);
      ctx.fillStyle = withAlpha(c.accent, 0.18);
      ctx.fillRect(a, padT, b - a, ph);
    }
    ctx.restore();

    if (!pts.length) {
      ctx.fillStyle = c.muted; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.font = '13px system-ui, sans-serif';
      ctx.fillText(rp.meta ? '该单位没有数据' : '读取中…', padL + pw / 2, padT + ph / 2);
    }
  }

  function rpDrawNav() {
    const c = rpColors();
    const { ctx, w, h } = rpCanvas($('rpNav'));
    ctx.clearRect(0, 0, w, h);
    if (!rp.full || !rp.full.points.length) return;
    const ext = rpExtent();
    const span = ext.end - ext.start;
    const pts = rp.full.points;
    const [ylo, yhi] = rpYRange(pts, ext.start, ext.end);
    const x = (t) => 4 + (t - ext.start) / span * (w - 8);
    const y = (v) => 4 + (1 - (v - ylo) / (yhi - ylo)) * (h - 8);
    rp.navGeom = { x, w, ext, span };
    rpDrawSeries(ctx, pts, x, y, c, { lineWidth: 1 });
    const vx0 = x(rp.view.start), vx1 = x(rp.view.end);
    ctx.fillStyle = withAlpha(c.panel, 0.55);
    ctx.fillRect(0, 0, vx0, h); ctx.fillRect(vx1, 0, w - vx1, h);
    ctx.strokeStyle = c.accent; ctx.lineWidth = 1.5;
    ctx.strokeRect(vx0 + 0.75, 0.75, Math.max(2, vx1 - vx0 - 1.5), h - 1.5);
    if (rp.sel) {
      ctx.fillStyle = withAlpha(c.accent, 0.18);
      ctx.fillRect(x(rp.sel.start), 0, Math.max(1, x(rp.sel.end) - x(rp.sel.start)), h);
    }
    const cx = Math.round(x(rp.cursor)) + 0.5;
    ctx.strokeStyle = c.warn; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(cx, 0); ctx.lineTo(cx, h); ctx.stroke();
  }

  function rpTick(now) {
    rp.raf = 0;
    if (!rp.open) return;
    if (rp.playing) {
      const dt = Math.min(0.25, (now - rp.lastFrame) / 1000);
      const ext = rpExtent();
      rp.cursor += dt * rp.speed;
      if (rp.cursor >= ext.end) {
        rp.cursor = ext.end;
        rp.playing = false;
        rpRenderPlay();
        rpScheduleStats(0);
      }
      // 视图跟随：光标保持在可见范围约 70% 处
      const span = rp.view.end - rp.view.start;
      if (span < ext.end - ext.start - 1e-9 && (rp.cursor > rp.view.start + span * 0.85 || rp.cursor < rp.view.start)) {
        rpSetView(rp.cursor - span * 0.7, rp.cursor + span * 0.3, { stats: false });
      }
      rp.dirty = true;
    }
    rp.lastFrame = now;
    if (rp.dirty) {
      rp.dirty = false;
      rpDraw();
      rpDrawNav();
      rpRenderReadout();
    }
    rp.raf = requestAnimationFrame(rpTick);
  }

  function rpTimeAtX(px) {
    const g = rp.geom;
    if (!g) return rp.view.start;
    return rp.view.start + (px - g.padL) / g.pw * (rp.view.end - rp.view.start);
  }

  function rpShowTip(px, py) {
    const tip = $('rpTip');
    const src = rpPointsFor();
    if (!src || !src.points.length || !rp.geom) { tip.classList.add('hidden'); return; }
    const t = rpTimeAtX(px);
    const p = src.points[rpIndexAt(src.points, t)];
    if (!p) { tip.classList.add('hidden'); return; }
    const u = rp.unit || '';
    let val;
    if (p[3] === null || p[3] === undefined) val = 'OL';
    else if (p[1] !== p[2]) {
      const a = fmtEng(p[1], u, 6), b = fmtEng(p[2], u, 6), m = fmtEng(p[3], u, 7);
      val = `${m.text} ${m.unit}<br><span class="tip-t">区间 ${a.text} ${a.unit} … ${b.text} ${b.unit}</span>`;
    } else { const m = fmtEng(p[3], u, 7); val = `${m.text} ${m.unit}`; }
    tip.innerHTML = `<div class="tip-v">${val}</div><div class="tip-t">${fmtWall(p[0])} · +${fmtElapsed(p[0] - rpOrigin(), 0.01)}</div>`
      + (p[5] ? '<div class="tip-t" style="color:var(--bad)">超限</div>' : '');
    tip.classList.remove('hidden');
    const wrap = $('rpChartWrap');
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    tip.style.left = Math.min(wrap.clientWidth - tw - 6, Math.max(6, px + 12)) + 'px';
    tip.style.top = Math.min(wrap.clientHeight - th - 6, Math.max(6, py - th - 8)) + 'px';
  }

  // ---- 交互 --------------------------------------------------------------
  (function bindReplay() {
    const cv = $('rpChart');
    const local = (e) => { const r = cv.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };

    cv.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      cv.setPointerCapture(e.pointerId);
      const p = local(e);
      const kind = (rp.mode === 'select' || e.shiftKey) ? 'select' : 'pan';
      rp.drag = { kind, x0: p.x, x1: p.x, view0: { ...rp.view }, moved: false };
      cv.classList.toggle('dragging', kind === 'pan');
      $('rpTip').classList.add('hidden');
    });
    cv.addEventListener('pointermove', (e) => {
      const p = local(e);
      if (!rp.drag) {
        rp.hover = p;
        rpShowTip(p.x, p.y);
        rp.dirty = true;
        return;
      }
      rp.drag.x1 = p.x;
      if (Math.abs(p.x - rp.drag.x0) > 3) rp.drag.moved = true;
      if (rp.drag.kind === 'pan' && rp.drag.moved && rp.geom) {
        const v0 = rp.drag.view0;
        const dt = (p.x - rp.drag.x0) / rp.geom.pw * (v0.end - v0.start);
        rpSetView(v0.start - dt, v0.end - dt);
      }
      rp.dirty = true;
    });
    const end = (e) => {
      if (!rp.drag) return;
      const d = rp.drag;
      rp.drag = null;
      cv.classList.remove('dragging');
      if (!d.moved) {
        rpSeek(rpTimeAtX(d.x0), { follow: false });   // 单击：定位播放位置
      } else if (d.kind === 'select') {
        const a = rpTimeAtX(Math.min(d.x0, d.x1)), b = rpTimeAtX(Math.max(d.x0, d.x1));
        const ext = rpExtent();
        rpSetSel({ start: Math.max(ext.start, a), end: Math.min(ext.end, b) });
      }
      rp.dirty = true;
    };
    cv.addEventListener('pointerup', end);
    cv.addEventListener('pointercancel', end);
    cv.addEventListener('pointerleave', () => { rp.hover = null; $('rpTip').classList.add('hidden'); rp.dirty = true; });
    cv.addEventListener('wheel', (e) => {
      e.preventDefault();
      const p = local(e);
      const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
      if (Math.abs(e.deltaX) > Math.abs(dy)) {
        const span = rp.view.end - rp.view.start;
        const dt = e.deltaX / (rp.geom ? rp.geom.pw : 800) * span;
        rpSetView(rp.view.start + dt, rp.view.end + dt);
      } else {
        rpZoom(Math.pow(1.0015, dy), rpTimeAtX(p.x));
      }
      rpShowTip(p.x, p.y);
    }, { passive: false });
    cv.addEventListener('dblclick', rpFit);

    // 导航条：点击/拖动移动视图
    const nav = $('rpNav');
    let navDrag = false;
    const navMove = (e) => {
      if (!rp.navGeom) return;
      const r = nav.getBoundingClientRect();
      const g = rp.navGeom;
      const t = g.ext.start + (e.clientX - r.left - 4) / (g.w - 8) * g.span;
      const span = rp.view.end - rp.view.start;
      rpSetView(t - span / 2, t + span / 2);
    };
    nav.addEventListener('pointerdown', (e) => { navDrag = true; nav.setPointerCapture(e.pointerId); navMove(e); });
    nav.addEventListener('pointermove', (e) => { if (navDrag) navMove(e); });
    nav.addEventListener('pointerup', () => { navDrag = false; });
    nav.addEventListener('pointercancel', () => { navDrag = false; });

    $('rpMode').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-mode]');
      if (!b) return;
      rp.mode = b.dataset.mode;
      for (const x of $('rpMode').querySelectorAll('button')) x.classList.toggle('active', x === b);
      $('rpChartWrap').classList.toggle('mode-select', rp.mode === 'select');
    });
    $('rpSpeed').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-speed]');
      if (!b) return;
      rp.speed = Number(b.dataset.speed);
      rpRenderPlay();
    });
    $('rpPlay').addEventListener('click', rpTogglePlay);
    $('rpFit').addEventListener('click', rpFit);
    $('rpClearSel').addEventListener('click', () => rpSetSel(null));
    $('rpZoomSel').addEventListener('click', () => {
      if (!rp.sel) return;
      const pad = (rp.sel.end - rp.sel.start) * 0.05;
      rpSetView(rp.sel.start - pad, rp.sel.end + pad);
    });
    $('rpScrub').addEventListener('input', () => {
      const ext = rpExtent();
      rpSeek(ext.start + Number($('rpScrub').value) / 10000 * (ext.end - ext.start));
    });
    $('rpExport').addEventListener('click', () => {
      if (!rp.name) return;
      const r = rp.sel || rp.view;
      const a = document.createElement('a');
      a.href = `/api/logs/${encodeURIComponent(rp.name)}/download?start=${r.start}&end=${r.end}`;
      a.setAttribute('download', '');
      document.body.appendChild(a); a.click(); a.remove();
    });
    $('rpReload').addEventListener('click', () => withBusy($('rpReload'), () => rpLoadFull(false)));
    $('rpClose').addEventListener('click', closeReplay);
    $('replayLayer').addEventListener('click', (e) => { if (e.target === $('replayLayer')) closeReplay(); });
    window.addEventListener('resize', () => { if (rp.open) { rp.dirty = true; rpScheduleDetail(); } });

    // 回放窗口内的快捷键（优先于全局快捷键）
    document.addEventListener('keydown', (e) => {
      if (!rp.open) return;
      if (e.key === 'Escape') { e.stopImmediatePropagation(); closeReplay(); return; }
      if (isTyping(e.target) && e.target.id !== 'rpScrub') { e.stopImmediatePropagation(); return; }
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const span = rp.view.end - rp.view.start;
      const step = e.shiftKey ? span / 4 : span / 40;
      switch (e.key) {
        case ' ': rpTogglePlay(); break;
        case 'ArrowRight': rpSeek(rp.cursor + step); break;
        case 'ArrowLeft': rpSeek(rp.cursor - step); break;
        case '+': case '=': rpZoom(1 / 1.6, rp.cursor); break;
        case '-': case '_': rpZoom(1.6, rp.cursor); break;
        case '0': rpFit(); break;
        case 'Home': rpSeek(rpExtent().start); break;
        case 'End': rpSeek(rpExtent().end); break;
        default: e.stopImmediatePropagation(); return;
      }
      e.preventDefault();
      e.stopImmediatePropagation();
    }, true);
  })();

  // ------------------------------------------------------------------
  // 初始化
  // ------------------------------------------------------------------
  async function init() {
    applyTheme(state.theme);
    state.statMetrics = loadStatMetrics();
    try {
      const w = parseInt(localStorage.getItem('dmm-window'), 10);
      if ([10, 30, 60, 300, 0].includes(w)) state.windowSec = w;
    } catch (_) {}
    setupChart();
    setZoomUI();
    try { if (localStorage.getItem('dmm-view') === 'table') setView('table'); } catch (_) {}
    setInterval(() => {
      renderLogClock();
      if (!document.hidden) renderReadout();   // 刷新停滞提示/时间
    }, 1000);
    renderStatsConfig();
    renderStats();
    requestAnimationFrame(chartTick);

    try {
      state.meta = await api('/api/meta');
      renderControls();
    } catch (err) {
      showToast('加载功能表失败：' + err.message, 'error');
    }
    // 功能专属设置（AC 滤波、输入阻抗等）来自高级设置表
    api('/api/advanced').then((d) => {
      if (!adv.schema) adv.schema = d.groups || [];
      applyFunctionLayout(true);
    }).catch(() => {});

    try {
      const st = await api('/api/state');
      state.sim = !!st.sim;
      applyState(st);
    } catch (_) { /* SSE 快照会补上 */ }

    connectStream();
    fetchLogs();
    setInterval(() => {
      if (!state.logging.active) return;
      fetchLogs();
    }, 5000);
  }

  init();
})();
