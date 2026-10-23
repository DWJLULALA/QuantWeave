/* Historical teaching only. Every structure is filtered by its availability. */
(() => {
  const esc=s=>window.esc(s), n=(v,d=2)=>v==null||!Number.isFinite(Number(v))?'—':Number(v).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d});
  let root=null,data=null,observer=null,request=0;
  let view={id:'bull2014',date:null,future:false,volume:'amount',strokes:true,segments:true,high:false};
  try{view={...view,...JSON.parse(localStorage.getItem('stocklab-chan-cases')||'{}')}}catch{}
  const save=()=>{try{localStorage.setItem('stocklab-chan-cases',JSON.stringify(view))}catch{}};
  const checks={same_rank_centres:'同级中枢',child_rank_matches_centres:'出口使用正确次级别',consecutive_downward_exits:'两个中枢向下离开',envelopes_descend_without_overlap:'完整波动范围下移',no_bootstrap_segment_in_centres:'不使用初始不完整线段',c_starts_at_B_exit:'末段对应最后中枢出口',third_sell:'反抽未回中枢',new_low:'末段创新低',trend_divergence:'面积与DIF同时减弱',next_sublevel_complete:'末段已完成',two_descending_subcentres:'末段内部两下降中枢',terminal_sublevel_divergence:'同一低点的次级别背驰',no_new_low_before_confirmation:'确认前未再破低'};
  function page(){return `<div class="hero"><div><h1>经典案例分析</h1><p>缠论 · 真实历史逐日回放 · 量价证据与失效检验</p></div><div class="hero-actions"><a class="btn ghost" href="#trends">返回趋势分析</a></div></div><div class="cc-root"><div class="panel"><div data-cc-loading role="status">正在加载经典案例…</div><div data-cc-body hidden>
    <div class="cc-controls"><label>行情案例<select id="cc-case" aria-label="行情案例"></select></label><label>关键时点<select id="cc-event" aria-label="关键时点"></select></label></div>
    <div class="cc-play"><button class="btn ghost" id="cc-prev" type="button">前一根</button><button class="btn ghost" id="cc-next" type="button">后一根</button><label><input type="checkbox" id="cc-future">显示后续价格（事后）</label><strong id="cc-date"></strong><span id="cc-asset" class="muted"></span></div>
    <label class="cc-slider-label" for="cc-day">截至此根收盘后可知</label><input id="cc-day" type="range" min="0" step="1" aria-label="回放截至K线">
    <div class="cc-options"><label><input id="cc-strokes" type="checkbox">冻结笔</label><label><input id="cc-segments" type="checkbox">递归线段与中枢</label><label><input id="cc-high" type="checkbox">此前20日高点</label><label>量能口径<select id="cc-volume"><option value="amount">成交额 / 过去20日均额</option><option value="volume">成交量 / 过去20日均量</option></select></label></div>
    <div class="cc-legend"><span class="cc-legend-price">K线：红涨 / 绿跌</span><span class="cc-legend-stroke">蓝：冻结笔</span><span class="cc-legend-segment">青：确认线段</span><span class="cc-legend-tail">黄虚线：未冻结末笔</span><span>◇：底分型确认日</span></div>
    <div class="cc-inspect" data-cc-inspect aria-live="off"></div><div data-cc-price class="cc-plot"></div>
    <div class="cc-metrics" aria-live="polite"><div><small>相对过去20日</small><strong data-cc-ratio></strong></div><div><small>日内收盘位置 CLV（−1～1）</small><strong data-cc-clv></strong></div><div><small>当日量价观察</small><strong data-cc-observation></strong></div></div>
    <div data-cc-volume class="cc-plot"></div><div class="cc-legend cc-score-key"><span class="cc-score-slow">红：平滑 α0.9</span><span class="cc-score-fast">黄：敏感规则</span><span>虚线：50分中线</span></div><div data-cc-score class="cc-plot"></div>
    <div data-cc-signals class="cc-signals"></div><details class="cc-lesson"><summary>案例解读（事后总结）</summary><p data-cc-lesson></p></details>
    <details class="cc-lesson"><summary>怎样读图：结构、倾向分和成交量</summary><p>原始K线处理包含后识别分型，再形成笔、特征序列线段及递归中枢。底分型只确认局部转折；严格一买还要求同级下降中枢、末端背驰、内部次级别证明及确认前不破低。</p><p>低点发生、结构确认和实际成交是三个时间。图中的一买位置只有到了确认时点才显示；之后破低保留原信号并标失效。日K递归层级不等同于实际分钟区间套。</p><p>红线是原平滑倾向分，黄线是敏感规则分；50分为评分中线，分数不是校准后的上涨概率。ADX和CHOP含趋势强度，强下跌也可能抬高评分。</p><p>量比和额比的基线均排除当日；CLV接近1表示收在日内高处，接近−1表示收在低处。放量反弹、放量突破和高成交回落是固定规则的教学观察，不进入严格一买定义，也不是自动买卖指令。巨量不等于净流入。</p><p>真实案例没有当时可得的E1＋E3＋E5＋E10预测，不能据此计算整套组合的历史胜率。模拟样例专门展示完整证明，不是真实行情。</p></details><p class="muted cc-method" data-cc-method></p>
    </div></div></div>`;}
  function current(){return data.cases.find(c=>c.id===view.id)||data.cases[0]}
  function index(){const c=current();const i=c.rows.findIndex(r=>r[1]===view.date);return i<0?Math.max(0,c.rows.findIndex(r=>r[1]===c.default)):i}
  function configure(){
    const c=current();view.id=c.id;
    root.querySelector('#cc-case').innerHTML=data.cases.map(c=>`<option value="${c.id}">${esc(c.name)}</option>`).join('');root.querySelector('#cc-case').value=c.id;
    root.querySelector('#cc-event').innerHTML='<option value="">逐根回放</option>'+c.events.map(([date,label])=>`<option value="${esc(date)}">${esc(date)} · ${esc(label)}</option>`).join('');
    for(const k of ['future','strokes','segments','high'])root.querySelector('#cc-'+k).checked=!!view[k];
    root.querySelector('#cc-volume').value=view.volume==='volume'?'volume':'amount';
    root.querySelector('#cc-day').max=String(c.rows.length-1);draw();
  }
  async function mount(){
    observer?.disconnect();root=document.querySelector('#page-chan_cases .cc-root');if(!root)return;
    const target=root,seq=++request;
    try{
      data=await window.api('/chan/classic-cases');if(seq!==request||root!==target||!target.isConnected)return;
      if(!data.available)throw Error(data.reason||'案例数据不可用');
      root.querySelector('[data-cc-loading]').hidden=true;root.querySelector('[data-cc-body]').hidden=false;
      configure();
      root.querySelector('#cc-case').onchange=e=>{view.id=e.target.value;view.date=null;configure();save()};
      root.querySelector('#cc-event').onchange=e=>{if(e.target.value){view.date=e.target.value;draw();save()}};
      root.querySelector('#cc-day').oninput=e=>{view.date=current().rows[Number(e.target.value)][1];draw()};root.querySelector('#cc-day').onchange=save;
      root.querySelector('#cc-prev').onclick=()=>step(-1);root.querySelector('#cc-next').onclick=()=>step(1);
      for(const k of ['future','strokes','segments','high'])root.querySelector('#cc-'+k).onchange=e=>{view[k]=e.target.checked;draw();save()};
      root.querySelector('#cc-volume').onchange=e=>{view.volume=e.target.value;draw();save()};
      let last=0,timer;observer=new ResizeObserver(entries=>{const w=entries[0].contentRect.width;if(Math.abs(w-last)<1)return;last=w;clearTimeout(timer);timer=setTimeout(()=>{if(root===target&&target.isConnected)draw()},80)});observer.observe(target);
    }catch(e){if(root===target)root.querySelector('[data-cc-loading]').textContent='案例加载失败：'+e.message;}
  }
  function step(delta){const c=current();view.date=c.rows[Math.max(0,Math.min(c.rows.length-1,index()+delta))][1];draw();save()}
  const path=(rows,x,y,col)=>rows.filter(r=>r[col]!=null).map((r,i)=>(i?'L':'M')+x(r[0]).toFixed(2)+' '+y(r[col]).toFixed(2)).join('');
  function frame(c,host,height,low,high,label,cut){
    const width=Math.max(160,Math.floor(host.clientWidth)),L=66,R=18,T=26,B=38;
    const x=i=>L+4+(i-c.rows[0][0])/(c.rows.at(-1)[0]-c.rows[0][0]||1)*(width-L-R-8);
    const y=v=>height-B-(v-low)/(high-low||1)*(height-B-T);
    const tickCount=width<360?2:width<500?3:4;
    const xt=Array.from({length:tickCount},(_,i)=>Math.round(i*(c.rows.length-1)/(tickCount-1)));
    let base=`<rect x="${L}" y="${T}" width="${width-L-R}" height="${height-B-T}" class="cc-frame"/>`;
    for(let j=0;j<4;j++){const v=low+(high-low)*j/3;base+=`<path class="cc-grid" d="M${L} ${y(v)}H${width-R}"/><text x="${L-7}" y="${y(v)+4}" text-anchor="end">${n(v,high-low<10?1:0)}</text>`}
    base+=xt.map((j,k)=>`<text x="${x(c.rows[j][0])}" y="${height-B+19}" text-anchor="${k===0?'start':k===xt.length-1?'end':'middle'}">${esc(c.synthetic?(width<280?'#'+(c.rows[j][0]+1):c.rows[j][1]):(width<280?c.rows[j][1].slice(5).replace('-','/'):c.rows[j][1].slice(2)))}</text>`).join('');
    base+=`<text x="${L}" y="15">${esc(label)}</text><text x="${width-R}" y="${height-1}" text-anchor="end">${c.synthetic?'样本序号':'交易日期'}</text>`;
    const clip='cc-clip-'+host.dataset.ccPlot;
    const defs=`<defs><clipPath id="${clip}"><rect x="${L+1}" y="${T+1}" width="${width-L-R-2}" height="${height-B-T-2}"/></clipPath></defs>`;
    const shade=view.future?`<rect x="${x(cut)}" y="${T}" width="${Math.max(0,width-R-x(cut))}" height="${height-B-T}" class="cc-future"/>`:'';
    return {width,height,L,R,T,B,x,y,base,defs,shade,clip};
  }
  function draw(){
    if(!root?.isConnected||!data)return;const c=current(),di=index(),r=c.rows[di],cut=r[0],known=c.rows.slice(0,di+1),shown=view.future?c.rows:known;
    view.date=r[1];root.querySelector('#cc-day').value=String(di);root.querySelector('#cc-date').textContent=r[1];root.querySelector('#cc-asset').textContent=c.asset;
    root.querySelector('#cc-event').value=c.events.some(e=>e[0]===r[1])?r[1]:'';root.querySelector('#cc-prev').disabled=di===0;root.querySelector('#cc-next').disabled=di===c.rows.length-1;
    const price=root.querySelector('[data-cc-price]');price.dataset.ccPlot='price';
    let min=Math.min(...shown.map(v=>v[4])),max=Math.max(...shown.map(v=>v[3]));
    if(view.high&&!c.synthetic){min=Math.min(min,...known.filter(v=>v[15]!=null).map(v=>v[15]));max=Math.max(max,...known.filter(v=>v[15]!=null).map(v=>v[15]))}
    const pad=(max-min)*.08||1,p=frame(c,price,310,min-pad,max+pad,c.synthetic?'模拟价格（无单位）':'上证指数（点）',cut),{x,y}=p;
    let marks=p.shade;
    if(view.future)marks+=`<path class="cc-after" d="${path(c.rows.slice(di),x,y,5)}"/>`;
    const cw=Math.max(.7,Math.min(7,(p.width-p.L-p.R)/c.rows.length*.6));
    if(c.rows.length<=400)marks+=known.map(d=>`<path class="cc-wick" d="M${x(d[0])} ${y(d[3])}V${y(d[4])}"/><rect class="cc-candle ${d[5]>=d[2]?'up':'down'}" x="${x(d[0])-cw/2}" y="${y(Math.max(d[2],d[5]))}" width="${cw}" height="${Math.max(.8,Math.abs(y(d[2])-y(d[5])))}"/>`).join('');
    else marks+=`<path class="cc-close" d="${path(known,x,y,5)}"/>`;
    if(view.strokes)marks+=c.strokes.filter(s=>s[4]<=cut).map(s=>`<path class="cc-stroke" d="M${x(s[0])} ${y(s[2])}L${x(s[1])} ${y(s[3])}"/>`).join('');
    if(r[9].length){const s=r[9];marks+=`<path class="cc-tail" d="M${x(s[0])} ${y(s[2])}L${x(s[1])} ${y(s[3])}"/>`}
    if(view.segments){
      marks+=c.centres.filter(z=>z.confirmed_idx<=cut).map(z=>`<rect class="cc-centre" x="${x(z.start_idx)}" y="${y(z.zg)}" width="${Math.max(1,x(z.end_idx)-x(z.start_idx))}" height="${Math.max(1,y(z.zd)-y(z.zg))}"/>`).join('');
      marks+=c.segments.filter(s=>s[4]<=cut).map(s=>`<path class="cc-segment" d="M${x(s[0])} ${y(s[2])}L${x(s[1])} ${y(s[3])}"/>`).join('');
    }
    if(view.high&&!c.synthetic)marks+=`<path class="cc-high" d="${path(known,x,y,15)}"/>`;
    marks+=c.fractals.filter(f=>f.available<=cut).map(f=>{const row=c.rows.find(v=>v[0]===f.available);return `<path class="cc-fractal" d="M${x(f.available)} ${y(row[5])-4}l4 4-4 4-4-4z"/>`}).join('');
    const buys=c.signals.filter(s=>s.strict_recursive&&s.confirmed_idx<=cut);
    marks+=buys.map(s=>{const expired=s.invalidated_idx!=null&&s.invalidated_idx<=cut;return `<g class="${expired?'cc-invalid':'cc-buy'}"><path d="M${x(s.point_idx)} ${y(s.stop)-5}l-5 10h10z"/><path class="cc-confirm" d="M${x(s.confirmed_idx)} ${p.T}V${p.height-p.B}"/><path class="cc-stop" d="M${x(s.confirmed_idx)} ${y(s.stop)}H${x(expired?s.invalidated_idx:cut)}"/></g>`}).join('');
    renderPlot(price,p,marks,c,cut,'price');
    const vol=root.querySelector('[data-cc-volume]');vol.dataset.ccPlot='volume';const col=view.volume==='volume'?12:13;
    const vmax=Math.max(1.4,...known.map(d=>d[col]||0))*1.12,volumeTitle=vol.clientWidth<430?(view.volume==='volume'?'量比20（倍）':'额比20（倍）'):(view.volume==='volume'?'成交量 / 前20日均量（倍）':'成交额 / 前20日均额（倍）'),vp=frame(c,vol,150,0,vmax,volumeTitle,cut);
    let vm=vp.shade+`<path class="cc-mid" d="M${vp.L} ${vp.y(1)}H${vp.width-vp.R}"/>`;
    if(!c.synthetic)vm+=known.filter(d=>d[col]!=null).map(d=>`<rect class="cc-vol ${d[5]>=d[2]?'up':'down'}" x="${vp.x(d[0])-cw/2}" y="${vp.y(d[col])}" width="${cw}" height="${Math.max(.4,vp.y(0)-vp.y(d[col]))}"/>`).join('');
    renderPlot(vol,vp,vm,c,cut,'volume');vol.hidden=!!c.synthetic;
    const score=root.querySelector('[data-cc-score]');score.dataset.ccPlot='score';const sp=frame(c,score,155,0,100,'趋势评分（分）',cut);
    renderPlot(score,sp,sp.shade+`<path class="cc-mid" d="M${sp.L} ${sp.y(50)}H${sp.width-sp.R}"/><path class="cc-slow" d="${path(known,sp.x,sp.y,6)}"/><path class="cc-fast" d="${path(known,sp.x,sp.y,7)}"/>`,c,cut,'score');score.hidden=!!c.synthetic;root.querySelector('.cc-score-key').hidden=!!c.synthetic;
    root.querySelector('[data-cc-ratio]').textContent=c.synthetic?'模拟样例无量能':`额比 ${n(r[13])} / 量比 ${n(r[12])}`;
    root.querySelector('[data-cc-clv]').textContent=c.synthetic?'—':n(r[14],3);
    root.querySelector('[data-cc-observation]').textContent=c.synthetic?'验证结构证明':r[19]?'高成交回落 · 分歧警示':r[18]?'放量突破此前20日高点':r[17]?'放量反弹 · 趋势待证':'未触发量价教学规则';
    root.querySelector('[data-cc-lesson]').textContent=c.lesson;
    const when=i=>c.rows.find(v=>v[0]===i)?.[1]||`窗口外第${i+1}根`;
    const proof=(s)=>`<ul>${Object.entries(s.evidence.checks).map(([k,v])=>`<li>${v?'✓':'待满足'} ${esc(checks[k]||k)}</li>`).join('')}</ul>${s.evidence.sublevel?`<details><summary>次级别递归证据</summary>${proof(s.evidence.sublevel)}</details>`:''}`;
    root.querySelector('[data-cc-signals]').innerHTML=buys.length?buys.map(s=>{const expired=s.invalidated_idx!=null&&s.invalidated_idx<=cut;return `<div class="notice ${expired?'warn':''}"><b>R${s.rank} 严格递归一买 · ${expired?'已失效':'截至本根仍有效'}${c.synthetic?' · 模拟验证':''}</b><p>低点 ${esc(when(s.point_idx))}｜确认 ${esc(when(s.confirmed_idx))}｜固定失效价 ${n(s.stop,3)}${expired?'｜破低 '+esc(when(s.invalidated_idx)):''}</p><details><summary>展开完整证明</summary>${proof(s)}</details></div>`}).join(''):'<div class="notice">截至当前回放时点，尚未确认严格递归一买。底分型与趋势评分不自动构成一买。</div>';
    root.querySelector('[data-cc-method]').textContent=`规则 ${data.rule_version} · ${c.synthetic?'明确模拟输入':`固定预热起点 ${data.meta.warmup}`} · 量能基线排除当日 · 所有判断收盘后可知，交易最早在下一可成交时点 · 教学回放不代表组合回测。`;
    inspect(r,c);
  }
  function inspect(r,c,after=false){root.querySelector('[data-cc-inspect]').textContent=`${after?'后续价格（事后） · ':''}${r[1]}　开 ${n(r[2])}　高 ${n(r[3])}　低 ${n(r[4])}　收 ${n(r[5])}${c.synthetic||after?'':`　成交额 ${n(r[11],0)} 亿元　成交量 ${n(r[10])} 亿股　平滑 ${n(r[6],1)} / 敏感 ${n(r[7],1)}`}`}
  function renderPlot(host,p,marks,c,cut,kind){
    host.innerHTML=`<svg class="cc-svg" viewBox="0 0 ${p.width} ${p.height}" role="img" aria-label="${kind==='price'?'价格与缠论结构':kind==='volume'?'相对成交量能':'趋势评分'}，截至${esc(view.date)}">${p.defs}${p.base}<g clip-path="url(#${p.clip})">${marks}<path class="cc-asof" d="M${p.x(cut)} ${p.T}V${p.height-p.B}"/></g><line class="cc-guide" y1="${p.T}" y2="${p.height-p.B}"/><rect data-cc-hit x="${p.L}" y="${p.T}" width="${p.width-p.L-p.R}" height="${p.height-p.T-p.B}" fill="transparent"/></svg>`;
    let pinned=false;
    const move=e=>{const rect=e.currentTarget.closest('svg').getBoundingClientRect(),px=(e.clientX-rect.left)/rect.width*p.width;
      const sample=Math.round(c.rows[0][0]+(px-p.L-4)/(p.width-p.L-p.R-8)*(c.rows.at(-1)[0]-c.rows[0][0]));
      const i=Math.max(0,Math.min(c.rows.length-1,sample-c.rows[0][0]));
      if(!view.future&&c.rows[i][0]>cut)return;inspect(c.rows[i],c,c.rows[i][0]>cut);
      root.querySelectorAll('.cc-guide').forEach(g=>{g.setAttribute('x1',px);g.setAttribute('x2',px);g.style.visibility='visible'});
    };
    const hit=host.querySelector('[data-cc-hit]');hit.onpointermove=e=>{if(!pinned)move(e)};hit.onclick=e=>{pinned=!pinned;move(e)};
    hit.onpointerleave=()=>{if(!pinned){root.querySelectorAll('.cc-guide').forEach(g=>g.style.visibility='hidden');inspect(current().rows[index()],current())}};
  }
  window.ChanCases={page,mount};
})();
