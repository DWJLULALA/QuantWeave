/* A real ranking explorer, linked to the single-stock chart workspace. */
(function () {
  'use strict';
  const ui = window.StockLabVisuals;
  if (!ui) return;
  const {get,escape:esc,num,fixed,date} = ui;
  const state = {industry:'', query:'', selected:'000001.SZ', data:null, sequence:0, options:[]};
  function industryList(data) {
    const supplied = data.industry_options;
    const entries = Array.isArray(supplied) ? supplied : (data.industries || []).map(x=>x.industry);
    return [...new Set(entries.map(x => typeof x === 'string' ? x : x.industry).filter(Boolean))].sort((a,b)=>a.localeCompare(b,'zh-CN'));
  }
  function spark(row) {
    const values = row.kronos_e || [];
    const valid = values.map(num).filter(x=>x !== null);
    if (!valid.length) return '<span class="muted">—</span>';
    const lo = Math.min(...valid), hi = Math.max(...valid), span = hi-lo || 1;
    let pen = false;
    const path = values.map((v,i) => { const n = num(v); if (n === null) { pen = false; return ''; } const move = pen ? 'L' : 'M'; pen = true; return `${move}${2+i/Math.max(values.length-1,1)*94},${27-(n-lo)/span*22}`; }).join(' ');
    return `<svg class="sd-spark" viewBox="0 0 100 32" role="img" aria-label="E1 至 E10 原始分数形状"><path d="${path}"/></svg>`;
  }
  function table(rows) {
    const query = state.query.trim().toLowerCase();
    const filtered = rows.filter(row => !query || `${row.ts_code} ${row.name || ''}`.toLowerCase().includes(query));
    if (!filtered.length) return '<div class="chart-empty">当前榜单中没有匹配股票</div>';
    return `<div class="table-wrap sd-ranking-scroll"><table class="sd-ranking-table"><thead><tr><th>排名</th><th>股票</th><th>行业</th><th>Kronos 分数</th><th>离散度</th><th>E1–E10 形状</th><th>查看</th></tr></thead><tbody>${filtered.map(row => `<tr data-symbol="${esc(row.ts_code)}" class="${row.ts_code === state.selected ? 'sd-selected' : ''}"><td><span class="sd-rank ${row.rank <= 3 ? 'sd-rank-top' : ''}">${esc(row.rank)}</span></td><td><button class="sd-stock-link" data-select-stock="${esc(row.ts_code)}"><strong>${esc(row.name || row.ts_code)}</strong><small>${esc(row.ts_code)}</small></button></td><td>${esc(row.industry || '未分类')}</td><td class="sd-numeric ${num(row.kronos_score) === null ? '' : row.kronos_score >= 0 ? 'sd-score-positive' : 'sd-score-negative'}">${fixed(row.kronos_score,4)}</td><td class="sd-numeric">${fixed(row.kronos_dispersion,4)}</td><td>${spark(row)}</td><td><button class="sd-open-stock" data-select-stock="${esc(row.ts_code)}" aria-label="查看 ${esc(row.name || row.ts_code)} K线">K 线 ↗</button></td></tr>`).join('')}</tbody></table></div><p class="sd-table-caption">显示 ${filtered.length} / ${rows.length} 只。点击股票名称或 K 线查看详情。</p>`;
  }
  function industries(rows) {
    if (!rows.length) return '<div class="chart-empty">暂无行业分布</div>';
    const sorted = [...rows].sort((a,b)=>(num(b.count)||0)-(num(a.count)||0));
    const max = Math.max(...sorted.map(row=>num(row.count)||0),1);
    return `<div class="sd-industry-bars">${sorted.slice(0,12).map(row=>`<button class="sd-industry-row" data-industry="${esc(row.industry || '')}" title="查看 ${esc(row.industry)}"><span>${esc(row.industry || '未分类')}</span><span class="sd-industry-track"><i style="width:${(num(row.count)||0)/max*100}%"></i></span><b>${esc(row.count)} 只</b></button>`).join('')}</div>`;
  }
  function renderRows(root) {
    const target = root.querySelector('#stock-ranking');
    if (!target || !state.data) return;
    target.innerHTML = table(state.data.rows || []);
    target.querySelectorAll('[data-select-stock]').forEach(button=>button.addEventListener('click',() => {
      state.selected = button.dataset.selectStock;
      ui.select(state.selected,true);
      target.querySelectorAll('tr[data-symbol]').forEach(row=>row.classList.toggle('sd-selected',row.dataset.symbol === state.selected));
    }));
  }
  async function load(root, force = false) {
    const sequence = ++state.sequence;
    root.querySelector('#stock-ranking').innerHTML = '<div class="chart-empty">读取真实 Kronos 股票排序…</div>';
    root.setAttribute('aria-busy','true');
    try {
      const data = await get('/market_snapshot?mode=snapshot&limit=100'+(state.industry ? '&industry='+encodeURIComponent(state.industry) : ''),force);
      if (sequence !== state.sequence || !root.isConnected) return;
      state.data = data;
      if (!state.industry || data.industry_options) state.options = industryList(data);
      const select = root.querySelector('#rank-industry');
      select.innerHTML = '<option value="">全部行业</option>'+state.options.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join('');
      select.value = state.industry;
      root.querySelector('#ranking-meta').textContent = `信号日 ${date(data.signal_date)} · ${state.industry || '全市场'} Top ${(data.rows || []).length}`;
      root.querySelector('#ranking-scope').textContent = data.industry_options ? '选择行业可查看该行业排名；搜索在当前榜单内匹配。' : '行业选项来自全市场 Top100；搜索在当前榜单内匹配。';
      root.querySelector('#industry-bars').innerHTML = industries(data.industries || []);
      root.querySelector('#industry-heading').textContent = `${state.industry || '当前榜单'}行业分布`;
      root.querySelectorAll('[data-industry]').forEach(button=>button.addEventListener('click',() => { state.industry = button.dataset.industry; state.query = ''; root.querySelector('#rank-search').value = ''; load(root); }));
      renderRows(root);
    } catch(error) {
      if (sequence === state.sequence && root.isConnected) root.querySelector('#stock-ranking').innerHTML = '<div class="notice error">股票排名读取失败：'+esc(error.message)+'</div>';
    } finally { if (sequence === state.sequence && root.isConnected) root.removeAttribute('aria-busy'); }
  }
  function mount() {
    const page = document.querySelector('#page-predict');
    if (!page?.classList.contains('active') || page.querySelector('#stock-ranking-panel')) return;
    const root = document.createElement('section'); root.className = 'panel sd-ranking'; root.id = 'stock-ranking-panel';
    root.innerHTML = `<div class="panel-head"><h2>全市场股票排序</h2><span id="ranking-meta" class="muted">读取信号日…</span></div><div class="sd-ranking-toolbar"><label>行业<select id="rank-industry"><option value="">全部行业</option>${state.options.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join('')}</select></label><label>股票搜索<input id="rank-search" placeholder="名称或代码" value="${esc(state.query)}"></label><button id="rank-refresh" class="btn">刷新排序</button></div><p class="sd-footnote" id="ranking-scope">选择行业查看排名，点击股票联动 K 线。</p><div class="sd-ranking-layout"><div id="stock-ranking"></div><aside><h3 id="industry-heading">当前榜单行业分布</h3><div id="industry-bars"></div><p class="sd-footnote">排名按 Kronos E1–E10 标准化分数均值排序；离散度为这 10 个分数的标准差。原始分数不是概率或收益率，也不等同于策略候选。</p></aside></div>`;
    page.append(root);
    root.querySelector('#rank-industry').value = state.industry;
    root.querySelector('#rank-industry').addEventListener('change',event => { state.industry = event.target.value; state.query = ''; root.querySelector('#rank-search').value = ''; load(root); });
    root.querySelector('#rank-search').addEventListener('input',event => { state.query = event.target.value; renderRows(root); });
    root.querySelector('#rank-refresh').addEventListener('click',()=>load(root,true));
    load(root);
  }
  window.addEventListener('stocklab:stock-selected',event=>{
    state.selected = event.detail.symbol;
    document.querySelectorAll('#stock-ranking tr[data-symbol]').forEach(row=>row.classList.toggle('sd-selected',row.dataset.symbol === state.selected));
  });
  document.addEventListener('DOMContentLoaded',()=>{
    const page = document.querySelector('#page-predict');
    if (page) new MutationObserver(mount).observe(page,{childList:true});
    mount();
  });
})();
