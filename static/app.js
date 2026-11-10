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

  function fmtEng(value, baseUnit, sig = 6) {
    if (value === null || value === undefined || !Number.isFinite(value)) {
      return { text: 'OL', unit: baseUnit };
    }
    let factor = 1, prefix = '';
    const abs = Math.abs(value);
    if (value !== 0) {
      for (const [f, p] of PREFIXES) {
        if (abs >= f) { factor = f; prefix = p; break; }
      }
      if (abs < 1e-12) { factor = 1e-12; prefix = 'p'; }
    }
    const scaled = value / factor;
    const a = Math.abs(scaled);
    let digits;
    if (a >= 100) digits = Math.max(0, sig - 3);
    else if (a >= 10) digits = Math.max(0, sig - 2);
    else digits = Math.max(0, sig - 1);
    let text = scaled.toFixed(Math.min(digits, 9));
    if (text.includes('.')) text = text.replace(/0+$/, '').replace(/\.$/, '');
    if (text === '-0') text = '0';
    return { text, unit: prefix + (baseUnit || '') };
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

  function showToast(msg, kind = '') {
    const el = $('toast');
    el.textContent = msg;
    el.className = 'toast ' + kind;
    el.classList.remove('hidden');
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => el.classList.add('hidden'), 3200);
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
    setText('deviceId', dev.idn || '--');

    const offline = st !== 'online';
    for (const id of ['rangeSelect', 'rateSelect', 'triggerSelect']) {
      const el = $(id);
      if (el) el.disabled = offline || state.switching;
    }
    $('syncState').disabled = false;
    for (const b of $('fnList').querySelectorAll('.fn-item')) b.disabled = offline;
    $('controlHint').textContent = offline
      ? '设备离线，设置不可用。'
      : (state.config.display_mode === 'current'
          ? '分流电流模式使用 DCV 档；点击其它功能会自动退出分流模式。'
          : '选择后立即生效，以仪器回读值为准。');
    if (st === 'online' && itLastDeviceState !== 'online') {
      itLastDeviceState = 'online';
      loadIntegrationTime();
    } else if (st !== 'online') {
      itLastDeviceState = st;
      const itEl = $('itReadback');
      if (itEl) itEl.textContent = '当前：—';
    }
    setZoomUI();
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
      const d = await api('/api/advanced/read?keys=itp,its');
      const itp = d.values && d.values.itp;
      const its = d.values && d.values.its;
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
    const value = parseFloat($('itValue').value);
    if (!Number.isFinite(value)) {
      showToast('请输入有效的积分时间', 'error');
      return;
    }
    const unit = $('itUnit').value;
    const key = unit === 'plc' ? 'itp' : 'its';
    const min = unit === 'plc' ? 0.02 : 0.00001;
    const max = unit === 'plc' ? 100 : 10;
    if (value < min || value > max) {
      showToast(`范围 ${min} – ${max} ${unit === 'plc' ? 'PLC' : 's'}`, 'error');
      return;
    }
    const btn = $('itApply');
    btn.disabled = true;
    try {
      await postJSON('/api/advanced/set', { key, value });
      showToast('积分时间已设置，已切换为自由采样', 'ok');
      await loadIntegrationTime();
      if (!$('advDrawer').classList.contains('hidden')) loadAdvKeys(['itp', 'its']);
    } catch (err) {
      showToast(err.message, 'error');
    } finally {
      btn.disabled = false;
    }
  }

  $('itApply').addEventListener('click', applyIntegrationTime);
  $('itUnit').addEventListener('change', () => {
    $('itValue').placeholder = $('itUnit').value === 'plc' ? '如 1.5' : '如 0.02';
  });

  function renderReadout(sample) {
    const s = sample || state.latest;
    const unitBase = baseUnit();
    const readout = $('readout');
    const alarm = (s && s.alarm) || state.alarm || 'off';
    readout.classList.toggle('alarm', alarm === 'low' || alarm === 'high' || alarm === 'ol');

    const code = state.instrument.function || (s && s.fn) || 'DCV';
    const meta = fnMeta(code);
    setText('pageTitle', meta ? meta.label : (state.instrument.function_label || '—'));
    setText('pageCode', code);
    setText('pageNote', meta ? (meta.note || '') : '');

    const uName = UNIT_NAMES[unitBase] || '读数';
    const badge = $('modeBadge');
    badge.textContent = state.config.display_mode === 'current' ? '分流电流' : uName;
    badge.className = 'badge ' + (unitBase === 'A' ? 'badge-current' : 'badge-voltage');

    setText('rangeLabel', state.instrument.range_label || '—');
    setText('rateLabel', state.instrument.rate_label || '—');
    setText('triggerLabel', state.instrument.trigger_label || '—');
    setText('shuntInfo', fmtR(state.config.shunt_ohm));

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
      const v = fmtEng(s.d, s.u || unitBase, 7);
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
      else { text = '正常'; }
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
        grid.innerHTML = '<div class="adv-note">请点击「设置」选择统计项</div>';
      } else {
        grid.innerHTML = metrics.map(key => {
          const def = STAT_DEFS.find(d => d.key === key);
          if (!def) return '';
          return `<div class="stat"><label>${def.label}</label><b>${formatStat(def, st, unit)}</b></div>`;
        }).join('');
      }
    }
    setText('sampleCount', String(st.n_total || 0));
    const rate = st.rate_hz;
    const online = (state.device.state || 'offline') === 'online';
    setText('sampleRate', rate ? `${Math.round(rate)} 样本/秒`
      : (online ? '等待数据' : '—'));
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

  function buildRangeOptions() {
    if (!state.meta) return;
    const sel = $('rangeSelect');
    const base = functionBaseUnit();
    const modeKey = base === 'V' ? 'voltage' : 'generic';
    if (sel.dataset.mode === modeKey) return;
    sel.dataset.mode = modeKey;
    sel.innerHTML = '';
    const items = modeKey === 'voltage'
      ? state.meta.ranges
      : [{ code: 'AUTO', label: '自动' },
         ...['R3', 'R4', 'R5', 'R6', 'R7'].map(c => ({ code: c, label: '档位 ' + c.slice(1) }))];
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
      btn.disabled = offline;
    }
  }

  async function switchFunction(code) {
    if ((state.device.state || 'offline') !== 'online') {
      showToast('设备离线，无法切换功能', 'error');
      return;
    }
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
    if (inst.range) {
      if (![...rangeSel.options].some(o => o.value === inst.range)) {
        const opt = document.createElement('option');
        opt.value = inst.range;
        opt.textContent = inst.range;
        rangeSel.appendChild(opt);
      }
      rangeSel.value = inst.range;
    }
    if (inst.rate) $('rateSelect').value = inst.rate;
    if (inst.trigger) $('triggerSelect').value = inst.trigger;

    const chips = [];
    if (inst.range_label) chips.push(`<span class="tag">量程 ${inst.range_label}</span>`);
    if (inst.rate_label) chips.push(`<span class="tag">速率 ${inst.rate_label}</span>`);
    if (inst.trigger_label) chips.push(`<span class="tag">触发 ${inst.trigger_label}</span>`);
    $('readbackChips').innerHTML = chips.join('');

    renderFunctionActive();
    renderDevice();
  }

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
      return;
    }
    const lim = (state.limits || {})[key] || {};
    if (!state.limitsDirty) {
      $('limitEnabled').checked = !!lim.enabled;
      $('limitLow').value = lim.low === null || lim.low === undefined ? '' : trimNum(lim.low);
      $('limitHigh').value = lim.high === null || lim.high === undefined ? '' : trimNum(lim.high);
    }
    updateLimitTag(state.alarm);
  }

  function renderLogging() {
    const lg = state.logging || {};
    const active = !!lg.active;
    $('logIndicator').classList.toggle('hidden', !active);
    $('logStart').disabled = active;
    $('logStop').disabled = !active;
    $('logStatus').textContent = active
      ? `记录中 · ${lg.rows || 0} 行`
      : (lg.file ? `已停止 · ${lg.rows || 0} 行` : '未记录');
    if (lg.error) $('logStatus').textContent += ' · 写入错误';
    $('logFileInfo').textContent = lg.file
      ? `${lg.file} · ${lg.rows || 0} 行${active ? ' · 正在写入' : ''}`
      : 'CSV 保存到 data/logs/';
  }

  // ------------------------------------------------------------------
  // 状态应用
  // ------------------------------------------------------------------
  function applyState(d) {
    if (!d) return;
    if (d.device) state.device = d.device;
    if (d.instrument && (!state.switching || !d.instrument.function
        || d.instrument.function === state.instrument.function)) {
      state.instrument = d.instrument;
    }
    if (d.config) state.config = d.config;
    if (d.limits) state.limits = d.limits;
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
    chart.dirty = true;
    if (adv.schema && !$('advDrawer').classList.contains('hidden')
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
    state.readoutDirty = true;
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
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
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
      if (bestD <= 40) { chart.cross = best; chart.dirty = true; }
      else if (chart.cross !== null) { chart.cross = null; chart.dirty = true; }
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
      setZoomUI();
      chart.cross = null;
      chart.dirty = true;
      ensureHistory();
    }, { passive: false });

    // 拖动平移
    chart.canvas.addEventListener('pointerdown', (ev) => {
      if (ev.button !== 0 || !chart.geom) return;
      drag = {
        x: ev.clientX,
        tMin: chart.geom.tMin,
        tMax: chart.geom.tMax,
        plotW: chart.geom.plotW,
        moved: false,
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
      setZoomUI();
      chart.dirty = true;
      ensureHistory();
    });
    const endDrag = (ev) => {
      if (!drag) return;
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

  function setZoomUI() {
    const live = state.view.live;
    const liveBtn = $('zoomLive');
    if (liveBtn) liveBtn.classList.toggle('active', live);
    for (const b of $('windowGroup').querySelectorAll('button')) {
      b.classList.toggle('active', live && parseInt(b.dataset.window, 10) === state.windowSec);
    }
    const hint = $('zoomHint');
    if (hint) {
      hint.textContent = live ? '滚轮缩放 · 拖动平移' : '已缩放 · 双击或点「实时」恢复';
    }
  }

  function resetZoomView() {
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
    setZoomUI();
    chart.cross = null;
    chart.dirty = true;
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
      const slice = points.slice(i, i + bucket);
      let lo = null, hi = null, hasOl = false;
      for (const p of slice) {
        if (p.d === null || p.d === undefined) { hasOl = true; continue; }
        if (!lo || p.d < lo.d) lo = p;
        if (!hi || p.d > hi.d) hi = p;
      }
      // 保留 OL 断点，避免把超量程区间连成假数据
      if (hasOl) {
        out.push({ t: slice[0].t, d: null, ol: true, u: slice[0].u, fn: slice[0].fn, raw: 'OL' });
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

    const colors = {
      ink: cssVar('--ink-2'), muted: cssVar('--muted'), grid: cssVar('--grid'),
      axis: cssVar('--axis'), panel: cssVar('--panel-2'),
      accent: state.config.display_mode === 'current' ? cssVar('--accent-current') : cssVar('--accent'),
      bad: cssVar('--bad'), warn: cssVar('--warn'),
    };
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
      emptyEl.textContent = dom.free ? '该时间段没有数据（拖动或缩小查看）' : '等待数据…';
      emptyEl.classList.remove('hidden');
      chart.visible = [];
      return;
    }
    emptyEl.classList.add('hidden');

    const numeric = pts.filter(p => p.d !== null && p.d !== undefined && Number.isFinite(p.d));
    if (!numeric.length) {
      emptyEl.textContent = '当前超量程（OL），等待有效数据…';
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
      for (let i = 1; i < pts.length; i++) diffs.push(pts[i].t - pts[i - 1].t);
      diffs.sort((a, b) => a - b);
      const med = diffs[Math.floor(diffs.length / 2)];
      if (med > 0) gap = Math.max(0.5, med * 4);
    }
    let prev = null;
    ctx.strokeStyle = colors.accent;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.beginPath();
    for (const p of drawPts) {
      if (p.d === null || p.d === undefined || !Number.isFinite(p.d)) {
        prev = null;
        continue;
      }
      const x = xOf(p.t), y = yOf(p.d);
      if (prev === null || p.t - prev > gap) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
      prev = p.t;
    }
    ctx.stroke();

    // 最新点
    const last = numeric[numeric.length - 1];
    const lx = xOf(last.t), ly = yOf(last.d);
    ctx.fillStyle = colors.panel;
    ctx.beginPath(); ctx.arc(lx, ly, 5, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = colors.accent;
    ctx.beginPath(); ctx.arc(lx, ly, 3.5, 0, Math.PI * 2); ctx.fill();

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
      const v = cp.s.d;
      const txt = v === null || v === undefined ? 'OL' : (fmtEng(v, unit, 6).text + ' ' + fmtEng(v, unit, 6).unit);
      tip.innerHTML = `<div>${timeStr(cp.s.t)}</div><div>${txt}</div>`;
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
    if (state.readoutDirty && !document.hidden && now - (chart.lastReadout || 0) >= 200) {
      chart.lastReadout = now;
      state.readoutDirty = false;
      renderReadout();
    }
    if (chart.dirty && !document.hidden && now - (chart.lastDraw || 0) >= 200) {
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
    const rows = state.samples.slice(-100).reverse().map(s => {
      const v = s.ol || s.d === null ? 'OL' : (() => { const x = fmtEng(s.d, s.u || unit, 6); return x.text + ' ' + x.unit; })();
      const st = s.alarm === 'low' ? '低于下限' : s.alarm === 'high' ? '高于上限' : s.alarm === 'ol' ? '超量程' : '正常';
      return `<tr><td>${timeStr(s.t)}</td><td>${v}</td><td>${st}</td></tr>`;
    });
    $('sampleTable').innerHTML = rows.join('');
  }
  setInterval(renderTable, 500);
  $('tableToggle').addEventListener('click', () => {
    state.tableOpen = !state.tableOpen;
    $('tableWrap').classList.toggle('hidden', !state.tableOpen);
    $('tableToggle').textContent = state.tableOpen ? '图表' : '表格';
    if (state.tableOpen) renderTable();
  });

  // ------------------------------------------------------------------
  // 控件事件
  // ------------------------------------------------------------------
  async function applySelect(el, action, label) {
    const requested = el.value;
    el.disabled = true;
    try {
      const result = await postJSON('/api/control', { action, value: requested });
      const applied = result.applied && result.applied[action];
      if (applied && applied !== requested) {
        showToast(`仪器未接受该${label}，当前为 ${applied}`, 'error');
        renderControls();
      } else {
        showToast(label + '已切换', 'ok');
      }
    } catch (err) {
      showToast(err.message, 'error');
      renderControls();
    } finally {
      el.disabled = false;
      renderDevice();
    }
  }

  $('rangeSelect').addEventListener('change', () => applySelect($('rangeSelect'), 'range', '量程'));
  $('rateSelect').addEventListener('change', () => applySelect($('rateSelect'), 'rate', '采样速率'));
  $('triggerSelect').addEventListener('change', () => applySelect($('triggerSelect'), 'trigger', '触发源'));

  $('syncState').addEventListener('click', async () => {
    try { await postJSON('/api/control', { action: 'sync' }); showToast('状态已刷新', 'ok'); }
    catch (err) { showToast(err.message, 'error'); }
  });

  $('modeGroup').addEventListener('click', async (ev) => {
    const btn = ev.target.closest('button');
    if (!btn) return;
    const mode = btn.dataset.mode;
    if (mode === state.config.display_mode) return;
    const r = (parseFloat($('shuntValue').value) || 0) * parseFloat($('shuntUnit').value);
    if (!(r > 0)) { showToast('请先填写有效的分流电阻', 'error'); return; }
    try {
      await postJSON('/api/config', { display_mode: mode, shunt_ohm: r });
      state.shuntDirty = false;
      state.limitsMode = null;
      renderLimits(true);
      showToast(mode === 'current' ? '已切换到分流电流模式' : '已切换到直接电压模式', 'ok');
    } catch (err) { showToast(err.message, 'error'); }
  });

  $('shuntValue').addEventListener('input', () => { state.shuntDirty = true; });
  $('shuntUnit').addEventListener('change', () => { state.shuntDirty = true; });

  $('applyShunt').addEventListener('click', async () => {
    const r = (parseFloat($('shuntValue').value) || 0) * parseFloat($('shuntUnit').value);
    if (!(r > 0)) { showToast('分流电阻必须大于 0', 'error'); return; }
    try {
      await postJSON('/api/config', { display_mode: state.config.display_mode, shunt_ohm: r });
      state.shuntDirty = false;
      showToast('分流设置已应用', 'ok');
    } catch (err) { showToast(err.message, 'error'); }
  });

  for (const id of ['limitLow', 'limitHigh']) {
    $(id).addEventListener('input', () => { state.limitsDirty = true; });
  }
  $('limitLowUnit').addEventListener('change', () => { state.limitsDirty = true; });
  $('limitHighUnit').addEventListener('change', () => { state.limitsDirty = true; });

  $('applyLimits').addEventListener('click', async () => {
    const mode = limitKey();
    if (!mode) { showToast('当前功能不支持限值报警', 'error'); return; }
    const lowRaw = $('limitLow').value;
    const highRaw = $('limitHigh').value;
    const low = lowRaw === '' ? null : parseFloat(lowRaw) * parseFloat($('limitLowUnit').value);
    const high = highRaw === '' ? null : parseFloat(highRaw) * parseFloat($('limitHighUnit').value);
    try {
      await postJSON('/api/limits', {
        mode, enabled: $('limitEnabled').checked, low, high,
      });
      state.limitsDirty = false;
      showToast('限值已应用', 'ok');
    } catch (err) { showToast(err.message, 'error'); }
  });

  $('statsReset').addEventListener('click', async () => {
    try { await postJSON('/api/stats/reset', {}); showToast('统计已重置', 'ok'); }
    catch (err) { showToast(err.message, 'error'); }
  });

  $('statsConfigBtn').addEventListener('click', () => {
    const panel = $('statsConfig');
    panel.classList.toggle('hidden');
    if (!panel.classList.contains('hidden')) renderStatsConfig();
  });
  $('statsConfigClose').addEventListener('click', () => $('statsConfig').classList.add('hidden'));
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

  $('logStart').addEventListener('click', async () => {
    try { const d = await postJSON('/api/log/start', {}); state.logging = d.logging; renderLogging(); fetchLogs(); }
    catch (err) { showToast(err.message, 'error'); }
  });
  $('logStop').addEventListener('click', async () => {
    try { const d = await postJSON('/api/log/stop', {}); state.logging = d.logging; renderLogging(); fetchLogs(); }
    catch (err) { showToast(err.message, 'error'); }
  });
  $('refreshLogs').addEventListener('click', fetchLogs);

  async function fetchLogs() {
    try {
      const d = await api('/api/logs');
      state.logging = d.logging || state.logging;
      renderLogging();
      const list = $('logList');
      list.innerHTML = '';
      if (!d.files.length) {
        list.innerHTML = '<div class="log-item"><span class="name">暂无记录文件</span></div>';
        return;
      }
      for (const f of d.files) {
        const div = document.createElement('div');
        div.className = 'log-item' + (f.active ? ' active' : '');
        const k = f.size > 1024 ? (f.size / 1024).toFixed(1) + ' KB' : f.size + ' B';
        div.innerHTML = `<span class="name">${f.name}</span>
          <span class="meta">${k}</span>
          <a href="/api/logs/${encodeURIComponent(f.name)}/download">下载</a>`;
        list.appendChild(div);
      }
    } catch (err) { showToast(err.message, 'error'); }
  }

  // 窗口切换
  $('windowGroup').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button');
    if (!btn) return;
    state.windowSec = parseInt(btn.dataset.window, 10);
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

  function advOpen() {
    $('advBackdrop').classList.remove('hidden');
    $('advDrawer').classList.remove('hidden');
    $('advDrawer').setAttribute('aria-hidden', 'false');
    loadAdvanced();
  }

  function advClose() {
    $('advBackdrop').classList.add('hidden');
    $('advDrawer').classList.add('hidden');
    $('advDrawer').setAttribute('aria-hidden', 'true');
  }

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
      summary.innerHTML = `<span class="adv-group-title">${group.label}</span>`;
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
  }

  function renderAdvItem(item) {
    const div = document.createElement('div');
    div.className = 'adv-item';
    div.dataset.key = item.key;

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
      btn.addEventListener('click', () => setAdvValue(item, input.value, readback));
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
        el.textContent = res.value;
        el.className = 'adv-readback ok';
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
      }
      showToast(item.label + ' 已设置', 'ok');
      if (item.key === 'itp' || item.key === 'its') loadIntegrationTime();
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
    if (item.confirm && !window.confirm(item.confirm)) return;
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '执行中…';
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
    await Promise.all(open.map(g => loadAdvGroup(g)));
    showToast('高级设置已刷新', 'ok');
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('advDrawer').classList.contains('hidden')) advClose();
  });

  // ------------------------------------------------------------------
  // 初始化
  // ------------------------------------------------------------------
  async function init() {
    applyTheme(state.theme);
    state.statMetrics = loadStatMetrics();
    setupChart();
    setZoomUI();
    renderStatsConfig();
    renderStats();
    requestAnimationFrame(chartTick);

    try {
      state.meta = await api('/api/meta');
      renderControls();
    } catch (err) {
      showToast('加载功能表失败：' + err.message, 'error');
    }

    try {
      const st = await api('/api/state');
      applyState(st);
      if (st.device && st.device.idn) {
        $('serverInfo').textContent = `设备 ${st.device.serial || ''} · 真机`;
      }
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
