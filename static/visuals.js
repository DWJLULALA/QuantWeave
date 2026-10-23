/* Real stock workspace: one response supplies candles, volume and model scores. */
(function () {
  'use strict';
  const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num = v => v === null || v === undefined || v === '' ? null : Number.isFinite(Number(v)) ? Number(v) : null;
  const fixed = (v, digits = 2) => num(v) === null ? '—' : Number(v).toFixed(digits);
  const date = v => { const s = String(v ?? '').slice(0, 10); return /^\d{8}$/.test(s) ? `${s.slice(0,4)}-${s.slice(4,6)}-${s.slice(6,8)}` : s || '—'; };
  const compact = v => num(v) === null ? '—' : Math.abs(v) >= 1e8 ? `${fixed(v / 1e8)}亿` : Math.abs(v) >= 1e4 ? `${fixed(v / 1e4)}万` : fixed(v, 0);
  const cache = new Map();
  const pending = new Map();
  async function get(path, force = false) {
    const cached = cache.get(path);
    if (!force && cached && Date.now() - cached.time < 120000) return cached.value;
    if (pending.has(path)) return pending.get(path);
    const request = (async () => {
      const response = await fetch('/api' + path);
      const body = await response.json();
      if (!response.ok || body.available === false) throw Error(body.error || body.detail || `读取失败（${response.status}）`);
      cache.set(path, {value: body, time: Date.now()});
      return body;
    })().finally(() => pending.delete(path));
    pending.set(path, request);
    return request;
  }
  const model = {symbol:'000001.SZ', data:null, days:60, ema:true, request:0};
  const api = window.StockLabVisuals = {get, escape, num, fixed, date, select};
  function empty(message) { return `<div class="chart-empty">${escape(message)}</div>`; }
  function bounds(values, zero = false) {
    const finite = values.map(num).filter(v => v !== null);
    if (!finite.length) return null;
    let lo = Math.min(...finite), hi = Math.max(...finite);
    if (zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    const padding = Math.max((hi - lo) * 0.08, Math.abs(hi) * 0.002, 0.001);
    return [lo - padding, hi + padding];
  }
  function linePath(rows, key, x, y) {
    let pen = false;
    return rows.map((r, i) => {
      const n = num(r[key]);
      if (n === null) { pen = false; return ''; }
      const move = pen ? 'L' : 'M'; pen = true;
      return `${move}${x(i).toFixed(2)},${y(n).toFixed(2)}`;
    }).join(' ');
  }
  function grid(lo, hi, y, x1, x2, count = 4) {
    return Array.from({length:count + 1}, (_, i) => {
      const value = lo + (hi - lo) * i / count;
      return `<line class="sd-grid" x1="${x1}" x2="${x2}" y1="${y(value)}" y2="${y(value)}"/><text x="${x1 - 9}" y="${y(value) + 4}" text-anchor="end">${fixed(value)}</text>`;
    }).join('');
  }
  function dateTicks(rows, x, y, key) {
    return [...new Set([0, Math.floor((rows.length - 1) / 2), rows.length - 1])].filter(i => i >= 0).map(i => `<text x="${x(i)}" y="${y}" text-anchor="${i === 0 ? 'start' : i === rows.length - 1 ? 'end' : 'middle'}">${escape(date(rows[i][key]))}</text>`).join('');
  }
  function candles(rows) {
    if (!rows.length) return empty('暂无日线行情');
    const b = bounds(rows.flatMap(r => [r.low, r.high, ...(model.ema ? [r.ema_close] : [])]));
    if (!b) return empty('没有完整价格数据');
    const [lo, hi] = b, left = 64, right = 914, top = 22, bottom = 255, volTop = 305, volBottom = 382;
    const x = i => left + (i + 0.5) * (right - left) / rows.length;
    const y = n => bottom - (n - lo) / (hi - lo) * (bottom - top);
    const volMax = Math.max(...rows.map(r => num(r.volume) ?? 0), 1);
    const width = Math.max(2, Math.min(12, (right - left) / rows.length * 0.64));
    const marks = rows.map((r, i) => {
      const o = num(r.open), h = num(r.high), l = num(r.low), c = num(r.close), v = num(r.volume);
      const tone = o !== null && c !== null && c >= o ? 'sd-up' : 'sd-down';
      const candle = [o,h,l,c].every(n => n !== null) ? `<line class="${tone}" x1="${x(i)}" x2="${x(i)}" y1="${y(h)}" y2="${y(l)}"/><rect class="${tone}" x="${x(i) - width / 2}" y="${Math.min(y(o),y(c))}" width="${width}" height="${Math.max(Math.abs(y(o)-y(c)),1.2)}"/>` : '';
      const volume = v !== null && v >= 0 ? `<rect class="${tone} sd-volume" x="${x(i) - width / 2}" y="${volBottom - v / volMax * (volBottom - volTop)}" width="${width}" height="${v / volMax * (volBottom - volTop)}"/>` : '';
      return candle + volume;
    }).join('');
    return `<svg class="sd-chart sd-candles" viewBox="0 0 960 414" role="img" aria-label="日线蜡烛图、EMA 收盘价与成交量；红涨绿跌" data-chart="candles" data-count="${rows.length}" tabindex="0">
      ${grid(lo, hi, y, left, right)}${marks}
      ${model.ema ? `<path class="sd-ema" d="${linePath(rows, 'ema_close', x, y)}"/>` : ''}
      <line class="sd-grid" x1="${left}" x2="${right}" y1="${volBottom}" y2="${volBottom}"/><text x="${left}" y="289">成交量 · 数据源原始单位</text><text x="${right}" y="289" text-anchor="end">最大 ${compact(volMax)}</text>
      ${dateTicks(rows,x,405,'timestamps')}<line class="sd-crosshair" x1="${x(rows.length-1)}" x2="${x(rows.length-1)}" y1="${top}" y2="${volBottom}"/>
    </svg>`;
  }
  function factorChart(rows) {
    const b = bounds(rows.flatMap(r => [r.kronos, ...(model.ema ? [r.kronos_ema] : [])]), true);
    if (!rows.length || !b) return empty('该标的暂无 Kronos 历史分数');
    const [lo,hi] = b, left = 64, right = 914, top = 20, bottom = 148;
    const x = i => left + (i + 0.5) * (right - left) / rows.length;
    const y = n => bottom - (n - lo) / (hi - lo) * (bottom - top);
    return `<svg class="sd-chart" viewBox="0 0 960 184" role="img" aria-label="Kronos 历史原始分数与 EMA；不是概率或收益率" data-chart="history" data-count="${rows.length}" tabindex="0">${grid(lo,hi,y,left,right,3)}<line class="sd-zero" x1="${left}" x2="${right}" y1="${y(0)}" y2="${y(0)}"/><path class="sd-score" d="${linePath(rows,'kronos',x,y)}"/>${model.ema ? `<path class="sd-ema" d="${linePath(rows,'kronos_ema',x,y)}"/>` : ''}${dateTicks(rows,x,175,'date')}<line class="sd-crosshair" x1="${x(rows.length-1)}" x2="${x(rows.length-1)}" y1="${top}" y2="${bottom}"/></svg>`;
  }
  function horizonChart(rows) {
    const b = bounds(rows.map(r => r.score), true);
    if (!rows.length || !b) return empty('该标的暂无 E1–E10 模型输出');
    const [lo,hi] = b, left = 64, right = 914, top = 20, bottom = 161;
    const x = i => left + (i + 0.5) * (right-left) / rows.length;
    const y = n => bottom - (n-lo) / (hi-lo) * (bottom-top);
    return `<svg class="sd-chart" viewBox="0 0 960 200" role="img" aria-label="Kronos E1 至 E10 原始分数；不是价格预测" data-chart="horizon" data-count="${rows.length}" tabindex="0">${grid(lo,hi,y,left,right,3)}<line class="sd-zero" x1="${left}" x2="${right}" y1="${y(0)}" y2="${y(0)}"/>${rows.map((r,i) => num(r.score) === null ? `<text x="${x(i)}" y="${y(0)-7}" text-anchor="middle">缺失</text>` : `<rect class="sd-horizon-bar ${r.score >= 0 ? 'sd-positive' : 'sd-negative'}" x="${x(i)-18}" width="36" y="${Math.min(y(r.score),y(0))}" height="${Math.max(1,Math.abs(y(r.score)-y(0)))}"/>`).join('')}<path class="sd-score" d="${linePath(rows,'score',x,y)}"/>${rows.map((r,i)=>`<text x="${x(i)}" y="190" text-anchor="middle">E${escape(r.horizon)}</text>`).join('')}<line class="sd-crosshair" x1="${x(rows.length-1)}" x2="${x(rows.length-1)}" y1="${top}" y2="${bottom}"/></svg>`;
  }
  function inspector(kind, row) {
    if (!row) return '暂无可读数据';
    if (kind === 'candles') return `${date(row.timestamps)}　开 ${fixed(row.open)}　高 ${fixed(row.high)}　低 ${fixed(row.low)}　收 ${fixed(row.close)}　EMA ${fixed(row.ema_close)}　量 ${compact(row.volume)}`;
    if (kind === 'history') return `${date(row.date)}　Kronos ${fixed(row.kronos,4)}　EMA ${fixed(row.kronos_ema,4)}`;
    return `E${row.horizon}　标准化原始分数 ${fixed(row.score,4)}　原始均值 ${fixed(row.raw_mean,4)}　原始标准差 ${fixed(row.raw_sigma,4)}`;
  }
  function bindInspectors(root, series) {
    root.querySelectorAll('[data-chart]').forEach(svg => {
      const rows = series[svg.dataset.chart] || [];
      let selected = Math.max(0, rows.length - 1);
      const inspect = index => {
        selected = Math.max(0,Math.min(rows.length - 1,index));
        const line = svg.querySelector('.sd-crosshair');
        if (line) { const x = 64 + (selected + 0.5) * 850 / rows.length; line.setAttribute('x1',x); line.setAttribute('x2',x); }
        const output = root.querySelector(`[data-inspector="${svg.dataset.chart}"]`);
        if (output) output.textContent = inspector(svg.dataset.chart, rows[selected]);
      };
      svg.addEventListener('pointermove',event => {
        const rect = svg.getBoundingClientRect(), x = (event.clientX-rect.left) / rect.width * 960;
        inspect(Math.floor((x - 64) / 850 * rows.length));
      });
      svg.addEventListener('keydown',event => {
        if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); inspect(selected + (event.key === 'ArrowLeft' ? -1 : 1)); }
      });
      inspect(selected);
    });
  }
  function render(root, data) {
    const rows = (data.ohlc || []).slice(-model.days), history = (data.factor_history || []).slice(-model.days), horizon = data.horizon || [];
    const last = rows.at(-1), previous = rows.at(-2), close = num(last?.close), previousClose = num(previous?.close);
    const change = close !== null && previousClose !== null && previousClose !== 0 ? (close / previousClose - 1) * 100 : null;
    const quote = root.querySelector('#sd-quote');
    quote.innerHTML = `<div class="sd-stock-identity"><h3>${escape(data.name || data.symbol)} <span>${escape(data.symbol)}</span></h3><span class="muted">${escape(data.industry || '未分类')} · 日线截至 ${escape(date(last?.timestamps))}</span></div><div class="sd-last-price ${change === null ? '' : change >= 0 ? 'sd-price-up' : 'sd-price-down'}"><strong>${fixed(close)}</strong><span>${change === null ? '涨跌幅暂无' : `${change >= 0 ? '+' : ''}${fixed(change)}%`}</span></div>`;
    root.querySelector('#sd-charts').innerHTML = `<div class="sd-chart-heading"><h3>日 K 线与成交量</h3><div class="sd-legend"><span class="sd-up-dot">红涨</span><span class="sd-down-dot">绿跌</span>${model.ema ? '<span class="sd-ema-dot">收盘价 EMA</span>' : ''}</div></div><div class="sd-inspector" data-inspector="candles" aria-live="off"></div>${candles(rows)}<div class="sd-chart-heading"><h3>Kronos 历史因子</h3><div class="sd-legend"><span class="sd-score-dot">E1–E10 均值</span>${model.ema ? '<span class="sd-ema-dot">因子 EMA</span>' : ''}</div></div><div class="sd-inspector" data-inspector="history" aria-live="off"></div>${factorChart(history)}<div class="sd-chart-heading"><h3>E1–E10 模型曲线</h3><span class="muted">信号日 ${escape(date(data.signal_date))}</span></div><div class="sd-inspector" data-inspector="horizon" aria-live="off"></div>${horizonChart(horizon)}<p class="sd-footnote">模型图展示标准化原始分数，不是上涨概率、收益率或目标价格。EMA 衰减系数为 ${escape(data.ema_decay ?? '—')}，用于观察曲线，不修改策略参数。移动鼠标或聚焦图表后按左右键查看数值。</p>`;
    root.querySelectorAll('[data-days]').forEach(button => button.classList.toggle('active',Number(button.dataset.days) === model.days));
    bindInspectors(root, {candles:rows, history, horizon});
  }
  async function load(root, force = false) {
    const seq = ++model.request, symbol = model.symbol;
    const field = root.querySelector('#stock-symbol'); if (field) field.value = symbol;
    root.querySelector('#sd-charts').innerHTML = empty(`正在读取 ${symbol} 的行情和模型曲线…`);
    root.querySelector('#sd-quote').textContent = symbol;
    root.setAttribute('aria-busy','true');
    try {
      const data = await get('/market_snapshot?mode=stock&symbol='+encodeURIComponent(symbol)+'&limit=90', force);
      if (seq !== model.request || !root.isConnected) return;
      model.data = data;
      render(root,data);
      window.dispatchEvent(new CustomEvent('stocklab:stock-selected',{detail:{symbol}}));
    } catch(error) {
      if (seq === model.request && root.isConnected) root.querySelector('#sd-charts').innerHTML = `<div class="notice error">${escape(error.message)}</div>`;
    } finally { if (root.isConnected && seq === model.request) root.removeAttribute('aria-busy'); }
  }
  function select(symbol, scroll = false) {
    const value = String(symbol || '').trim().toUpperCase();
    const root = document.querySelector('#stock-visuals');
    if (!root) return;
    if (!/^\d{6}\.(SH|SZ|BJ)$/.test(value)) { root.querySelector('#sd-query-error').textContent = '请输入代码及交易所，例如 000001.SZ 或 600000.SH'; return; }
    root.querySelector('#sd-query-error').textContent = '';
    model.symbol = value;
    load(root);
    if (scroll) root.scrollIntoView({behavior:'smooth',block:'start'});
  }
  function mount() {
    const page = document.querySelector('#page-predict');
    if (!page?.classList.contains('active') || page.querySelector('#stock-visuals')) return;
    const root = document.createElement('section');
    root.id = 'stock-visuals'; root.className = 'panel sd-workspace';
    root.innerHTML = `<div class="panel-head"><h2>单股行情与模型详情</h2><span class="pill">真实行情</span></div><form class="sd-toolbar" id="sd-query-form"><label class="sd-symbol-label" for="stock-symbol">股票代码</label><input id="stock-symbol" value="${escape(model.symbol)}" placeholder="000001.SZ" autocomplete="off" spellcheck="false"><button class="btn primary" type="submit">查看股票</button><button class="btn ghost" id="sd-reload" type="button">刷新数据</button><div class="sd-range" role="group" aria-label="展示交易日数">${[30,60,90].map(n=>`<button type="button" data-days="${n}" class="${n === model.days ? 'active' : ''}">${n} 日</button>`).join('')}</div><label class="sd-toggle"><input id="sd-ema" type="checkbox" ${model.ema ? 'checked' : ''}>显示 EMA</label></form><div class="sd-query-error" id="sd-query-error" role="status"></div><div id="sd-quote" class="sd-quote"></div><div id="sd-charts"></div>`;
    const ranking = page.querySelector('#stock-ranking-panel');
    if (ranking) page.insertBefore(root,ranking); else page.append(root);
    root.querySelector('#sd-query-form').addEventListener('submit',event => { event.preventDefault(); select(root.querySelector('#stock-symbol').value); });
    root.querySelector('#sd-reload').addEventListener('click',() => load(root,true));
    root.querySelector('#sd-ema').addEventListener('change',event => { model.ema = event.target.checked; if (model.data && model.data.symbol === model.symbol) render(root,model.data); });
    root.querySelectorAll('[data-days]').forEach(button => button.addEventListener('click',() => { model.days = Number(button.dataset.days); if (model.data && model.data.symbol === model.symbol) render(root,model.data); }));
    load(root);
  }
  api.mount = mount;
  document.addEventListener('DOMContentLoaded',() => {
    const page = document.querySelector('#page-predict');
    if (page) new MutationObserver(mount).observe(page,{childList:true});
    mount();
  });
})();
