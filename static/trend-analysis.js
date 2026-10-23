/* Observed market/industry trends. No portfolio or prediction parameters. */
(() => {
  const periods={W:'周线',D:'日线','60F':'60分钟','30F':'30分钟',M:'月线'};
  const esc=value=>window.esc(value);
  const num=(v,n=2)=>v==null||!Number.isFinite(Number(v))?'—':Number(v).toLocaleString('zh-CN',{minimumFractionDigits:n,maximumFractionDigits:n});
  const localTime=t=>String(t||'—').replace('T',' ').replace(/\+08:00$/,'');
  const pct=v=>v==null?'—':(Number(v)*100).toFixed(2)+'%';
  const phase=v=>({'1':'上涨','0':'盘整','-1':'下跌'})[String(v)]||'—';
  const phaseClass=v=>v===1?'ok':v===-1?'bad':'warn';
  let saved={};try{saved=JSON.parse(localStorage.getItem('stocklab-trend-view')||'{}')}catch{}
  const view={tab:saved.tab==='industries'?'industries':'markets',level:periods[saved.level]?saved.level:'D',
    count:Math.max(40,Math.min(1000,Number(saved.count)||160)),industry:saved.industry||null,
    classification:['sw1','sw2','csi10'].includes(saved.classification)?saved.classification:'sw1',parent:'',query:'',bottomFilter:'all',rankMode:saved.rankMode==='complete'?'complete':'reference',left:saved.left||'000001',right:saved.right||'000905',sort:'trend_score',descending:true,structureRank:[2,3,4].includes(saved.structureRank)?saved.structureRank:2};
  let snapshot=null,root=null,sequence=0,refreshing=false,observer=null;
  const cache=new Map();
  function remember(){try{localStorage.setItem('stocklab-trend-view',JSON.stringify(view))}catch{}}
  const loading=message=>`<div class="trend-empty" role="status">${esc(message)}</div>`;
  function page(legacy){return `<div class="trend-page-root">
    <div class="trend-topline"><div class="trend-tabs" role="tablist" aria-label="趋势分析范围"><button type="button" id="trend-market-tab" role="tab" aria-controls="trend-markets" data-trend-tab="markets" aria-selected="${view.tab==='markets'}">大盘趋势</button><button type="button" id="trend-industry-tab" role="tab" aria-controls="trend-industries" data-trend-tab="industries" aria-selected="${view.tab==='industries'}">行业趋势与排名</button></div><button class="btn ghost" id="trend-refresh">刷新趋势数据</button></div>
    <div id="trend-status" class="trend-status muted" role="status" aria-live="polite">读取最近趋势快照…</div>
    <div class="trend-shared-controls"><label>结构周期 <select id="trend-period">${Object.entries(periods).map(([v,n])=>`<option value="${v}" ${v===view.level?'selected':''}>${n}</option>`).join('')}</select></label><label class="trend-range-label"><span>观察 <b id="trend-count">${view.count}</b> 根K线</span><input type="range" id="trend-window" min="40" max="1000" value="${view.count}" aria-label="观察K线数量"></label><label>递归层级 <select id="trend-structure-rank"><option value="2">R2中枢 / R1线段</option><option value="3">R3中枢 / R2线段</option><option value="4">R4中枢 / R3线段</option></select></label><span class="muted trend-key"><i class="trend-solid"></i>确认笔 <i class="trend-dashed"></i>末笔／延伸 <i class="trend-zone-key"></i>笔重叠观察区 · 紫=递归线段 · 绿框=递归中枢</span></div>
    <div id="trend-markets" role="tabpanel" aria-labelledby="trend-market-tab" ${view.tab==='markets'?'':'hidden'}>
      <div class="trend-pair"><div class="panel"><label class="trend-select-label">左侧指数 <select id="trend-left"></select></label><div id="trend-left-chart">${loading('加载上证指数结构…')}</div></div><div class="panel"><label class="trend-select-label">右侧指数 <select id="trend-right"></select></label><div id="trend-right-chart">${loading('加载中证500结构…')}</div></div></div>
      <p class="muted trend-method">递归线段经特征序列确认，已确认结构固定；虚框为发展中枢，最右端虚笔仍可延伸。空心买点位于历史低点，竖线标实际结构确认时间；确认前不可使用。仅使用已经结束的K线；周线、月线排除当前未完成周期。</p>
      <div class="trend-legacy">${legacy}</div>
    </div>
    <div id="trend-industries" role="tabpanel" aria-labelledby="trend-industry-tab" ${view.tab==='industries'?'':'hidden'}>
      <div class="panel"><div class="panel-head"><div><h2>行业趋势排名</h2><span class="muted" id="trend-rank-asof">中证全指行业指数 · 统一收盘日期</span></div></div><div class="trend-industry-controls"><label>行业分类 <select id="trend-classification"><option value="sw1">申万一级 · 31类</option><option value="sw2">申万二级 · 134类</option><option value="csi10">中证全指 · 10类</option></select></label><label id="trend-parent-control" hidden>所属一级 <select id="trend-parent"><option value="">全部一级行业</option></select></label><label>搜索行业 <input id="trend-search" type="search" placeholder="名称或代码" autocomplete="off"></label><label>底部观察 <select id="trend-bottom-filter"><option value="all">全部状态</option><option value="strict">严格递归1买 · 仍有效</option><option value="confirmed">底分型形态已确认</option><option value="standing">分型上沿已站稳 · 2收盘</option><option value="watch_1buy">底分型＋背驰候选</option><option value="waiting">低点待确认</option><option value="data_gap">数据不足</option></select></label><label>排名范围 <select id="trend-rank-mode"><option value="reference">参考榜 · 包含缺口数据</option><option value="complete">完整行情榜</option></select></label></div><div id="trend-bottom-overview" class="trend-bottom-overview"></div><div id="trend-ranking">${loading('读取行业趋势榜…')}</div><details class="trend-rank-method"><summary>排名口径与数据覆盖</summary><p id="trend-rank-method" class="muted"></p><p class="muted">趋势分是当前结构强弱的相对排序，不是上涨概率。事后浪段标签不进入排名；若某行业缺少同一收盘日行情或近60日数据不连续，将从榜单排除并注明。</p></details></div>
      <div class="panel" id="trend-industry-detail"><div class="panel-head"><h2 id="trend-industry-title">行业结构与浪型倾向</h2><span class="muted">点击榜单中的行业切换</span></div><div id="trend-industry-chart">${loading('请选择行业')}</div><div id="trend-industry-wave"></div></div>
    </div>
  </div>`;}
  function tab(){
    root.querySelectorAll('[data-trend-tab]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.trendTab===view.tab)));
    root.querySelector('#trend-markets').hidden=view.tab!=='markets';root.querySelector('#trend-industries').hidden=view.tab!=='industries';
    loadCharts();remember();
  }
  async function mount(){
    if(observer)observer.disconnect();root=document.querySelector('#page-trends .trend-page-root');if(!root)return;
    root.addEventListener('click',event=>{
      const tabButton=event.target.closest('[data-trend-tab]');if(tabButton){view.tab=tabButton.dataset.trendTab;tab();return}
      const row=event.target.closest('[data-trend-industry]');if(row){view.industry=row.dataset.trendIndustry;renderRanking();loadCharts().then(()=>root?.querySelector('#trend-industry-detail')?.scrollIntoView({block:'start',behavior:matchMedia('(prefers-reduced-motion:reduce)').matches?'auto':'smooth'}));remember();return}
      const sort=event.target.closest('[data-trend-sort]');if(sort){const key=sort.dataset.trendSort;view.descending=view.sort===key?!view.descending:true;view.sort=key;renderRanking();return}
      if(event.target.closest('#trend-refresh'))refreshData();
    });
    root.querySelector('#trend-structure-rank').value=view.structureRank;
    root.querySelector('#trend-structure-rank').addEventListener('change',event=>{view.structureRank=Number(event.target.value);redraw();remember()});
    root.querySelector('#trend-period').addEventListener('change',event=>{view.level=event.target.value;loadCharts();remember()});
    root.querySelector('#trend-window').addEventListener('input',event=>{view.count=Number(event.target.value);root.querySelector('#trend-count').textContent=view.count;redraw()});
    root.querySelector('#trend-window').addEventListener('change',remember);
    for(const side of ['left','right'])root.querySelector('#trend-'+side).addEventListener('change',event=>{view[side]=event.target.value;loadCharts();remember()});
    root.querySelector('#trend-classification').value=view.classification;
    root.querySelector('#trend-rank-mode').value=view.rankMode;
    for(const [id,key] of [['classification','classification'],['parent','parent'],['bottom-filter','bottomFilter'],['rank-mode','rankMode']])root.querySelector('#trend-'+id).addEventListener('change',event=>{view[key]=event.target.value;if(key==='classification'){view.parent='';view.industry=null;updateClassification()}else{renderRanking();loadCharts()}remember()});
    root.querySelector('#trend-search').addEventListener('input',event=>{view.query=event.target.value.trim();renderRanking()});
    let timer,observedWidth=-1;observer=new ResizeObserver(entries=>{const width=entries[0]?.contentRect.width||0;if(Math.abs(width-observedWidth)<.5)return;observedWidth=width;clearTimeout(timer);timer=setTimeout(redraw,100)});observer.observe(root);
    await loadOverview();
  }
  async function loadOverview(){
    const target=root;
    try{const value=await window.api('/trends');if(root!==target||!document.contains(target))return;snapshot=value;
      if(!value.available){root.querySelector('#trend-status').textContent=value.reason||'暂无趋势数据';root.querySelector('#trend-ranking').innerHTML=loading('点击“刷新趋势数据”获取行业行情');return}
      root.querySelector('#trend-status').textContent=`数据截止：${value.asof} · 快照更新 ${value.generated_at.replace('T',' ').slice(0,19)} · 只使用已收盘K线`;
      for(const side of ['left','right'])root.querySelector('#trend-'+side).innerHTML=value.markets.map(r=>`<option value="${r.code}" ${r.code===view[side]?'selected':''}>${esc(r.name)} ${r.code}</option>`).join('');
      updateClassification();
    }catch(error){if(root===target)root.querySelector('#trend-status').textContent='趋势数据读取失败：'+error.message;}
  }
  function group(){return snapshot?.classifications?.[view.classification]||{name:snapshot?.industry_taxonomy,industries:snapshot?.industries||[],reference_industries:snapshot?.industries||[],excluded:snapshot?.excluded||[],requested:snapshot?.industry_requested||0,catalog:[]}}
  function rankRows(){const g=group();return view.rankMode==='complete'?g.industries:(g.reference_industries||g.industries)}
  function updateClassification(){
    if(!snapshot?.available)return;const g=group();
    const parent=root.querySelector('#trend-parent');root.querySelector('#trend-parent-control').hidden=view.classification!=='sw2';
    parent.innerHTML='<option value="">全部一级行业</option>'+Object.values(snapshot.sw_catalog?.industries||[]).filter(r=>r.taxonomy==='sw1').map(r=>`<option value="${r.code}">${esc(r.name)}</option>`).join('');parent.value=view.parent;
    root.querySelector('#trend-rank-asof').textContent=g.name+' · '+snapshot.asof+' · '+g.requested+'类目录';
    root.querySelector('#trend-rank-method').textContent=snapshot.ranking_method+'。分位数与趋势名次仅在当前分类和排名范围内计算；筛选父级不改变全二级名次。基准：沪深300。结构项使用冻结的R2中枢，证据不足时退到冻结R1线段方向。'+snapshot.p_method+'。完整行情榜要求最近61个基准交易日与结构历史输入均无缺口；参考榜仅作观察，RS严格使用20/60个基准交易日端点，结构与P会受缺口影响。';
    const rows=rankRows();if(!rows.some(r=>r.code===view.industry))view.industry=rows[0]?.code||g.catalog?.[0]?.code;
    renderRanking();loadCharts();remember();
  }
  function renderRanking(){
    if(!snapshot?.available||!root)return;const g=group(),all=rankRows();
    const good=all.filter(r=>r.bottom?.fractal_confirmed),watch=good.filter(r=>r.bottom?.status==='watch_1buy'),gap=all.filter(r=>!r.data_quality?.complete);
    root.querySelector('#trend-bottom-overview').innerHTML=`<div><strong>${good.length}</strong><span>日线底分型形态已确认</span></div><div><strong>${good.filter(r=>r.bottom?.upper_reclaimed_2closes).length}</strong><span>已连续2收盘站上分型上沿</span></div><div><strong>${watch.length}</strong><span>其中背驰候选 · 1买观察</span></div><div><strong>${all.filter(r=>r.bottom?.strict_1buy_confirmed).length}</strong><span>严格递归1买 · 日K基底 · 仍有效</span></div><p class="muted">目录 ${g.requested} 类 · 完整榜 ${g.industries.length} 类 · 参考榜 ${(g.reference_industries||g.industries).length} 类。${gap.length?`当前参考数据中 ${gap.length} 类存在缺口，暂停底部确认。`:''}底分型是局部形态。严格递归1买要求同级两中枢全波动区间下移、趋势背驰、末端次级别递归完成；日K递归层级不等同于真实30分钟区间套。${snapshot.repair_summary?`本次补入 ${snapshot.repair_summary.filled_bars} 根真实K线；尚缺 ${snapshot.repair_summary.remaining_bars} 根。开高低收价争议窗口暂停严格确认。`:""}</p>`;
    const q=view.query.toLowerCase();const rows=all.filter(r=>(!view.parent||r.parent_code===view.parent)&&(!q||(r.name+r.code).toLowerCase().includes(q))&&(view.bottomFilter==='all'||(view.bottomFilter==='strict'?r.bottom?.strict_1buy_confirmed:view.bottomFilter==='confirmed'?r.bottom?.fractal_confirmed:view.bottomFilter==='standing'?r.bottom?.upper_reclaimed_2closes:view.bottomFilter===r.bottom?.status))).sort((a,b)=>((Number(a[view.sort])||0)-(Number(b[view.sort])||0))*(view.descending?-1:1)||a.code.localeCompare(b.code));
    const columns=[['trend_score','趋势分'],['p_impulse','P(推动)·平滑'],['rs20','20日相对强弱'],['rs60','60日相对强弱']];
    const header=([k,n])=>`<th aria-sort="${view.sort===k?(view.descending?'descending':'ascending'):'none'}"><button data-trend-sort="${k}">${n}${view.sort===k?(view.descending?' ↓':' ↑'):''}</button></th>`;
    root.querySelector('#trend-ranking').innerHTML=`${gap.length&&view.rankMode==='reference'?'<p class="trend-data-notice" role="note">参考榜含缺失交易日：分数仅供观察，不据此确认底部或1买。可切换“完整行情榜”查看可核验范围。</p>':''}<p class="muted">显示 ${rows.length} / ${all.length} 类 · ${view.rankMode==='reference'?'参考名次':'完整行情名次'}</p><div class="table-wrap"><table class="trend-rank-table"><thead><tr><th>趋势名次</th><th>行业 / 所属一级</th>${header(columns[0])}<th>日线 / 周线</th><th>日线底部 / 严格递归1买</th>${columns.slice(1).map(header).join('')}<th>120日位置</th></tr></thead><tbody>${rows.map(r=>`<tr class="${r.code===view.industry?'trend-selected':''}"><td>${r.rank}</td><td><button class="trend-industry-link" data-trend-industry="${r.code}" aria-pressed="${r.code===view.industry}">${esc(r.name)}<small>${r.code}${r.parent_name?' · '+esc(r.parent_name):''}</small></button></td><td><span class="trend-score-value">${num(r.trend_score,1)}</span><span class="trend-score-track"><i style="width:${Math.max(0,Math.min(100,r.trend_score))}%"></i></span></td><td><span class="pill ${phaseClass(r.phase_d)}">${phase(r.phase_d)}</span> <span class="pill ${phaseClass(r.phase_w)}">${phase(r.phase_w)}</span></td><td><span class="pill ${r.bottom?.fractal_confirmed?'ok':'warn'}">${esc(r.bottom?.label||'待计算')}</span>${r.bottom?.fractal_confirmed&&r.bottom?.confirm_dt?`<small class="trend-confirm-date">确认 ${esc(r.bottom.confirm_dt.slice(5,10))}</small>`:''}${r.bottom?.upper_reclaimed_2closes?'<small class="trend-confirm-date">2收盘站上分型上沿</small>':''}${r.bottom?.strict_signals?.length?r.bottom.strict_signals.map(b=>`<small class="trend-strict-label">R${b.rank} 1买 ${esc(b.point_at.slice(0,10))}<br>确认 ${esc(b.confirmed_at.slice(0,10))} · 失效价 ${num(b.stop)}</small>`).join(''):`<small class="trend-confirm-date">${r.bottom?.recursive_assessment==='data_gap'?'输入缺口':r.bottom?.recursive_assessment==='insufficient_structure'?'递归趋势证据不足':'无有效严格递归1买'}</small>`}</td><td>${pct(r.p_impulse)}</td><td class="${r.rs20>=0?'red':'green'}">${pct(r.rs20)}</td><td class="${r.rs60>=0?'red':'green'}">${pct(r.rs60)}</td><td>${pct(r.position120)}</td></tr>`).join('')||'<tr><td colspan="10">当前条件下没有可排名行业。数据缺口不会以插值补齐。</td></tr>'}</tbody></table></div><details class="trend-unranked"><summary>查看 ${g.excluded.length} 类未进入完整榜的行业与原因（仍可点击看图）</summary>${g.excluded.map(r=>{const c=g.catalog?.find(c=>c.code===r.code);return `<p><button class="trend-industry-link" data-trend-industry="${r.code}">${esc(c?.name||r.code)} ${r.code}</button> <span class="muted">${esc(r.reason)}</span></p>`}).join('')}</details>`;
  }
  async function getAsset(code){const key=snapshot.generation+':'+code+':'+view.level;if(!cache.has(key)){const result=await window.api('/trends/'+code+'?level='+view.level);if(result.available&&result.generation!==snapshot.generation)throw Error('数据正在更新，请刷新后重试');cache.set(key,result)}return cache.get(key)}
  async function loadCharts(){
    if(!snapshot?.available||!root)return;
    const sw=view.tab==='industries'&&view.classification!=='csi10';
    for(const o of root.querySelector('#trend-period').options)o.disabled=sw&&['30F','60F'].includes(o.value);
    if(sw&&['30F','60F'].includes(view.level)){view.level='D';root.querySelector('#trend-period').value='D'}
    const token=++sequence,target=root;
    const requests=view.tab==='markets'?[[view.left,'trend-left-chart'],[view.right,'trend-right-chart']]:[[view.industry,'trend-industry-chart']];
    await Promise.allSettled(requests.map(async([code,id])=>{
      const el=target.querySelector('#'+id);if(!code)return;el.innerHTML=loading('加载'+periods[view.level]+'结构…');
      try{const data=await getAsset(code);if(token!==sequence||root!==target)return;el._trendData=data;renderChart(el,data);
        if(id==='trend-industry-chart'){
          target.querySelector('#trend-industry-title').textContent=(data.summary?.name||'行业')+' · 结构与浪型倾向';
          const w=target.querySelector('#trend-industry-wave');w.innerHTML=data.available?`<h3>每日浪型倾向与指数位置</h3>${window.interactiveFactorChart(data.wave,[['p_impulse','P(推动浪) 平滑(α0.9)','var(--red)'],['p_fast','P(推动浪) 敏感(快速规则)','var(--amber)'],['label','事后浪段（仅复盘）','var(--muted2)'],['index_pos','本行业·120日位置','var(--cyan)'],['csi300_pos','沪深300·120日位置','#60a5fa']],true,.5)}<p class="muted">P为七规则倾向值；行业与基准位置按近120日收盘价区间归一化。灰色浪段可能随后续行情改变，不参与趋势分。</p>`:'';
          w.querySelectorAll('.ifchart').forEach(box=>window.ifcRender(box));
        }
      }catch(error){if(token===sequence&&root===target)el.innerHTML=loading('暂无法读取：'+error.message)}
    }));
  }
  function redraw(){if(!root)return;root.querySelectorAll('[id$="-chart"]').forEach(el=>{if(el._trendData&&el.offsetWidth)renderChart(el,el._trendData)})}
  const checkNames={same_rank_centres:'同级两个中枢',consecutive_downward_exits:'两个相邻中枢均向下离开',envelopes_descend_without_overlap:'两中枢全波动区间下移且不重叠',no_bootstrap_segment_in_centres:'中枢不含起始试探段',c_starts_at_B_exit:'c段从末中枢离开',third_sell:'次级别反抽不回中枢',new_low:'c段创新低',trend_divergence:'MACD负面积与DIF同时背驰',next_sublevel_complete:'特征序列确认c段完成',two_descending_subcentres:'c段内有两个下降次级别中枢',terminal_sublevel_divergence:'末端次级别背驰递归确认',no_new_low_before_confirmation:'确认前未跌破低点'};
  function recursiveDetail(rec,s){
    if(!rec)return '';
    const chosen=view.structureRank,z=rec.counts?.closed_centres?.[chosen]||0,lines=rec.counts?.segments?.[chosen-1]||0;
    const events=(rec.signals||[]).filter(b=>b.rank===chosen),latest=rec.diagnostics?.filter(b=>b.rank===chosen).at(-1);
    const statuses={active:'仍有效',invalidated:'已跌破失效',withdrawn:'数据修订撤回（保留原记录）',monitoring_gap:'确认后行情缺口，失效状态待核验',monitoring_conflict:'确认后源间价差，失效状态待核验'};
    return `<div class="trend-recursive-detail"><b>递归结构核验 · R${chosen}</b><p>${esc(rec.scope)} 当前频率已冻结 R${chosen-1}线段 ${lines} 条，已结束 R${chosen}中枢 ${z} 个。${rec.assessment==='data_gap'?'输入仍有缺口，暂停严格确认。':z<2?'尚未形成足够的同级趋势中枢。':'逐项核验趋势及末端次级别完成。'}</p><div class="trend-signal-list">${events.map(b=>`<article class="trend-signal-card"><b>R${b.rank} 严格递归1买 · ${statuses[b.status]||b.status}</b><dl><div><dt>低点时间</dt><dd>${esc(b.point_at)}</dd></div><div><dt>结构确认时间</dt><dd>${esc(b.confirmed_at)}</dd></div><div><dt>首次实际观测</dt><dd>${esc(localTime(b.observed_at))}</dd></div><div><dt>固定失效价</dt><dd>${num(b.stop)}${b.invalidated_at?' · '+esc(b.invalidated_at)+'跌破':''}</dd></div></dl><p>${b.backfilled?'历史补算：确认时间仅表示该历史收盘后可知；当时未实际发布。':'新增观测：以首次实际观测时间为准。'}</p></article>`).join('')||'<p class="muted">本层没有可确认的严格递归1买记录；底分型与笔级背驰不升级为确认买点。</p>'}</div><details><summary>证据、数据补缺与防重绘口径</summary><p>同级下降中枢要求后中枢 GG &lt; 前中枢 DD；c段须创新低、负MACD面积缩小且DIF抬高，并经特征序列冻结；末端次级别须递归背驰完成。上游有缺口的特征序列使用保守的反向序列确认。末端虚笔和发展中枢不用于已确认信号。</p>${latest?`<p>最近本层未通过：${esc(latest.point_at)} 低点；${latest.failed_checks.map(k=>esc(checkNames[k]||k)).join('、')}。</p>`:''}<p>本周期另有 ${rec.floor_candidates?.length||0} 个笔级基底历史候选，未满足完整递归要求；${rec.blocked_candidates?.length||0} 个信号因行情缺口或高低价冲突被拦截。补入 ${s.data_quality?.repaired_dates?.length||0} 根真实日K；检测到 ${s.data_quality?.conflict_count||0} 个官方/补源OHLC差异，保留官方原行。历史始于 ${esc(rec.history_start||'固定起点')}，规则 ${esc(rec.rule_version)}。</p><p>确认时间使用最后必要证据的实际可知时间。价格跌破固定失效价时追加失效事件；数据修订追加撤回事件，保留原始观测。未收盘日K、未完成周月线不进入确认。真实分钟区间套：${rec.intraday_nesting_verified?'已验证':'未验证；申万历史分钟数据尚不可用'}。</p><small>输入指纹 ${esc(rec.input_sha256||'')}</small></details></div>`;
  }
  function renderChart(el,payload){
    if(!payload.available){el.innerHTML=loading(payload.reason||'暂无可用结构');return}
    const openedDetails=Array.from(el.querySelectorAll('details'),d=>d.open);
    const c=payload.chart,s=payload.summary,rec=c.recursive,rank=view.structureRank,data=c.bars.slice(-view.count),width=Math.floor(el.clientWidth);
    if(width<180)return;const L=66,R=18,T=26,H=315,B=40,MH=130;
    const lo=Math.min(...data.map(d=>d.low),...(c.zone?[c.zone.zd]:[])),hi=Math.max(...data.map(d=>d.high),...(c.zone?[c.zone.zg]:[])),pad=(hi-lo)*.09||1;
    const x=i=>L+4+(i+.5)*(width-L-R-8)/data.length,y=p=>H-B-(p-lo+pad)/(hi-lo+2*pad)*(H-B-T);
    const parse=t=>Date.parse(t.replace(' ','T')+':00+08:00');
    const timeline=c.timeline.map(parse),base=timeline.indexOf(parse(data[0].dt));
    const toX=t=>{const time=parse(t);let left=0,right=timeline.length;while(left<right){const m=(left+right)>>1;if(timeline[m]<time)left=m+1;else right=m}return x(left-base)};
    const macValues=data.flatMap(d=>[d.dif,d.dea,d.hist,0]),ml=Math.min(...macValues),mh=Math.max(...macValues),mp=(mh-ml)*.12||1;
    const my=v=>MH-B-(v-ml+mp)/(mh-ml+2*mp)*(MH-B-T);
    const bw=Math.max(.7,Math.min(8,(width-L-R)/data.length*.6));
    const uid='trend-'+s.code+'-'+Math.random().toString(16).slice(2,8),clip=uid+'-clip';
    const ticks=(min,max,n)=>{const span=max-min||1,raw=span/n,mag=Math.pow(10,Math.floor(Math.log10(raw))),v=raw/mag,step=(v<1.5?1:v<3?2:v<7?5:10)*mag;const out=[];for(let x=Math.ceil(min/step)*step;x<=max;x+=step)out.push(x);return out};
    const dateLabel=d=>d.dt.slice(data[0].dt.slice(0,4)===data.at(-1).dt.slice(0,4)?5:0,10);
    const tickCount=width<420?3:4;
    const xt=Array.from({length:tickCount},(_,i)=>Math.round(i*(data.length-1)/(tickCount-1)));
    function frame(height,ys,min,max,title){return `<rect x="${L}" y="${T}" width="${width-L-R}" height="${height-B-T}" class="trend-frame"/>${ticks(min,max,4).map(v=>`<path d="M${L} ${ys(v)}H${width-R}" class="gridline"/><text x="${L-8}" y="${ys(v)+4}" text-anchor="end">${num(v,0)}</text>`).join('')}${xt.map((j,k)=>`<text x="${x(j)}" y="${height-B+20}" text-anchor="${k===0?'start':k===xt.length-1?'end':'middle'}">${dateLabel(data[j])}</text>`).join('')}<text x="${L}" y="15">${title}</text><text x="${width-R}" y="${height-2}" text-anchor="end">交易日期</text>`}
    const zone=c.zone?`<rect x="${Math.max(L,toX(c.zone.start))}" y="${y(c.zone.zg)}" width="${Math.max(0,width-R-Math.max(L,toX(c.zone.start)))}" height="${Math.max(.5,y(c.zone.zd)-y(c.zone.zg))}" class="trend-zone"/>`:'';
    const candle=data.map((d,i)=>`<path d="M${x(i)} ${y(d.high)}V${y(d.low)}" class="trend-wick"/><rect x="${x(i)-bw/2}" y="${y(Math.max(d.open,d.close))}" width="${bw}" height="${Math.max(.8,Math.abs(y(d.open)-y(d.close)))}" class="trend-candle ${d.close>=d.open?'up':'down'}"/>`).join('');
    const strokes=c.strokes.filter(a=>parse(a.end)>=parse(data[0].dt)&&!(a.provisional&&c.live?.replaces_last_provisional)).map(a=>`<path d="M${toX(a.start)} ${y(a.start_price)}L${toX(a.end)} ${y(a.end_price)}" class="trend-bi ${a.provisional?'provisional':''}"/>`).join('');
    const live=c.live?`<path d="M${toX(c.live.start)} ${y(c.live.start_price)}L${toX(c.live.end)} ${y(c.live.end_price)}" class="trend-bi provisional"/>`:'';
    const maxBar=data.reduce((a,b)=>a.high>b.high?a:b),minBar=data.reduce((a,b)=>a.low<b.low?a:b);
    const extreme=[[maxBar,'high',-8],[minBar,'low',17]].map(([d,k,dy])=>{const px=x(data.indexOf(d));return `<text x="${px}" y="${y(d[k])+dy}" text-anchor="${px>width-105?'end':px<L+70?'start':'middle'}">${num(d[k])}</text>`}).join('');
    const path=key=>data.map((d,i)=>(i?'L':'M')+x(i)+' '+my(d[key])).join('');
    const bars=data.map((d,i)=>`<rect x="${x(i)-bw/2}" y="${my(Math.max(0,d.hist))}" width="${bw}" height="${Math.max(.6,Math.abs(my(d.hist)-my(0)))}" class="trend-hist"/>`).join('');
    const recursiveZones=(rec?.centres?.[rank]||[]).filter(z=>parse(z.end)>=parse(data[0].dt)).map(z=>`<rect x="${Math.max(L,toX(z.start))}" y="${y(z.zg)}" width="${Math.max(0,Math.min(width-R,toX(z.end))-Math.max(L,toX(z.start)))}" height="${Math.max(.7,y(z.zd)-y(z.zg))}" class="trend-recursive-zone ${z.provisional?'developing':''}"><title>R${rank}中枢 ${num(z.zd)}—${num(z.zg)}；${z.provisional?'发展中':esc(z.confirmed_at)+'确认'}</title></rect>`).join('');
    const segments=(rec?.layers?.[rank-1]||[]).filter(a=>parse(a.end)>=parse(data[0].dt)).map(a=>`<path d="M${toX(a.start)} ${y(a.start_price)}L${toX(a.end)} ${y(a.end_price)}" class="trend-segment"><title>R${rank-1}线段；端点 ${esc(a.end)}；冻结 ${esc(a.confirmed_at)}</title></path>`).join('');
    const buys=(rec?.signals||[]).filter(b=>b.rank===rank&&parse(b.confirmed_at)>=parse(data[0].dt)&&b.status!=='withdrawn').map(b=>{const px=toX(b.point_at),cx=toX(b.confirmed_at),py=y(b.stop),stopEnd=b.invalidated_at?Math.min(width-R,toX(b.invalidated_at)):width-R;return `<g class="trend-buy ${b.status==='invalidated'?'invalidated':''}"><title>R${rank}严格递归1买；低点 ${esc(b.point_at)}；确认 ${esc(b.confirmed_at)}；首次观测 ${esc(localTime(b.observed_at))}；失效价 ${num(b.stop)}；${b.backfilled?'历史补算':'新增观测'}；${b.status==='invalidated'?'已于'+esc(b.invalidated_at)+'失效':'仍有效'}</title><path d="M${px} ${py-5}l-5 9h10z"/><line x1="${cx}" x2="${cx}" y1="${T}" y2="${H-B}" class="trend-buy-confirm"/><path d="M${cx} ${py}H${stopEnd}" class="trend-buy-stop"/><text x="${Math.max(L+3,Math.min(width-R-65,cx+4))}" y="${T+14}">1买确认</text></g>`}).join('');
    const last=data.at(-1),zoneText=c.zone?`${num(c.zone.zd)}—${num(c.zone.zg)}`:'尚无三笔重叠区';
    el.innerHTML=`<div class="trend-chart-heading"><b>${esc(s.name)}</b><strong>${num(last.close)} <small>点</small></strong></div><div class="muted trend-chart-meta">${esc(periods[c.level])} · ${esc(c.asof)} · <span class="pill ${phaseClass(c.phase)}">${phase(c.phase)}</span></div><div class="trend-bottom-detail"><b>${esc(c.bottom?.label||'底部待核验')}</b><p>近期低点 ${num(c.bottom?.low)} · ${esc(c.bottom?.low_dt||'—')}　${c.bottom?.fractal_confirmed&&c.bottom?.confirm_dt?'形态确认于 '+esc(c.bottom.confirm_dt):c.bottom?.status==='data_gap'?'数据不完整，暂停确认':'右侧结构尚未确认'} · 距低点 ${pct(c.bottom?.rebound)}</p><p>${esc(c.bottom?.next_step||'')}。${c.bottom?.fractal_confirmed?'分型上沿 '+num(c.bottom.fractal_upper)+'，近两次收盘'+(c.bottom.upper_reclaimed_2closes?'已站上':'尚未同时站上')+'。':''}${rec?.evaluated?`严格递归1买：${rec.signals.filter(b=>b.status==='active').length} 个仍有效（当前K线基底）。`:'严格递归确认暂停。'}</p>${c.data_quality?.missing_dates?.length?`<p class="trend-data-notice">截至本周期的结构输入缺 ${c.data_quality.missing_dates.length} 个基准交易日：${esc(c.data_quality.missing_dates.slice(-8).join('、'))+(c.data_quality.missing_dates.length>8?'…':'')}。本图有缺口，周期合成与底部判断均待补齐。</p>`:''}<details><summary>核验依据</summary><p>${esc(c.bottom?.method||'')}</p>${c.bottom?.momentum?`<p>下降笔负MACD面积比 ${num(c.bottom.momentum.negative_area_ratio,3)}；DIF ${num(c.bottom.momentum.dif_previous)} → ${num(c.bottom.momentum.dif_current)}。只作笔级动能比较。</p>`:''}<p>${s.taxonomy?'申万行业支持日／周／月；源站没有可靠的历史30／60分钟OHLC。':''}</p><a href="${esc(s.data_quality?.source||'#')}" target="_blank" rel="noopener">查看原始日线来源</a></details></div><div class="muted trend-chart-zone">笔重叠观察区：${zoneText}</div><div class="trend-inspect" aria-live="polite">${esc(last.dt)}　开 ${num(last.open)}　高 ${num(last.high)}　低 ${num(last.low)}　收 ${num(last.close)}</div>
      <svg viewBox="0 0 ${width} ${H}" class="trend-svg" role="img" aria-label="${esc(s.name)}${periods[c.level]}K线和缠论结构"><defs><clipPath id="${clip}"><rect x="${L+1}" y="${T+1}" width="${width-L-R-2}" height="${H-B-T-2}"/></clipPath></defs>${frame(H,y,lo-pad,hi+pad,'指数（点）')}<g clip-path="url(#${clip})">${zone}${recursiveZones}${candle}${strokes}${live}${segments}${buys}${extreme}</g><line class="trend-guide" y1="${T}" y2="${H-B}"/><rect class="trend-hit" data-trend-hit="price" x="${L}" y="${T}" width="${width-L-R}" height="${H-B-T}"/></svg>
      <div class="trend-macd-key"><span>DIF ─</span><span>DEA ┄</span><span>MACD柱</span></div><svg viewBox="0 0 ${width} ${MH}" class="trend-svg" role="img" aria-label="MACD动能">${frame(MH,my,ml-mp,mh+mp,'MACD（点）')}${bars}<path d="${path('dif')}" class="trend-dif"/><path d="${path('dea')}" class="trend-dea"/><line class="trend-guide" y1="${T}" y2="${MH-B}"/><rect class="trend-hit" data-trend-hit="macd" x="${L}" y="${T}" width="${width-L-R}" height="${MH-B-T}"/></svg>${recursiveDetail(rec,s)}`;
    el.querySelectorAll('details').forEach((detail,i)=>{detail.open=!!openedDetails[i]});
    let pinned=false;
    function inspect(event){const svg=event.currentTarget.closest('svg'),rect=svg.getBoundingClientRect(),px=(event.clientX-rect.left)*width/rect.width;const j=Math.max(0,Math.min(data.length-1,Math.round((px-L-4)/(width-L-R-8)*data.length-.5))),d=data[j];
      el.querySelector('.trend-inspect').textContent=`${d.dt}　开 ${num(d.open)}　高 ${num(d.high)}　低 ${num(d.low)}　收 ${num(d.close)}　DIF ${num(d.dif)}／DEA ${num(d.dea)}`;
      el.querySelectorAll('.trend-guide').forEach(g=>{g.setAttribute('x1',px);g.setAttribute('x2',px);g.style.visibility='visible'});
    }
    el.querySelectorAll('[data-trend-hit]').forEach(hit=>{hit.addEventListener('pointermove',event=>{if(!pinned)inspect(event)});hit.addEventListener('click',event=>{pinned=!pinned;inspect(event)});hit.addEventListener('pointerleave',()=>{if(!pinned)el.querySelectorAll('.trend-guide').forEach(g=>g.style.visibility='hidden')})});
  }
  async function refreshData(){
    if(refreshing||!root)return;refreshing=true;const target=root,button=target.querySelector('#trend-refresh');button.disabled=true;
    const status=target.querySelector('#trend-status');status.textContent='正在刷新官方指数与行业数据，保留上次快照…';
    try{const job=await window.api('/jobs',{method:'POST',body:JSON.stringify({kind:'trend_refresh',params:{}})});
      for(let tries=0;tries<180;tries++){
        if(root!==target||!document.contains(target))return;
        const result=await window.api('/runs/'+job.id);
        if(result.status==='completed'){cache.clear();await loadOverview();if(window.loadMarketWave)window.loadMarketWave();return}
        if(['failed','error'].includes(result.status))throw Error(result.error||'刷新失败；已保留上次快照');
        await new Promise(resolve=>setTimeout(resolve,1500));
      }status.textContent='刷新仍在运行，可在运行日志查看进度。';
    }catch(error){status.textContent='趋势刷新未完成：'+error.message}finally{refreshing=false;if(document.contains(button))button.disabled=false}
  }
  window.TrendAnalysis={page,mount};
})();
