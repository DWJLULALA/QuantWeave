#!/usr/bin/env node
/* Capture real demo interactions. No response interception or data substitution. */
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const args = process.argv.slice(2);
function arg(key, fallback) { const i = args.indexOf(key); return i < 0 ? fallback : args[i + 1]; }
const out = path.resolve(arg('--work-dir', 'promo-work'));
const base = arg('--base-url', 'http://127.0.0.1:7072/');
const sceneNames = arg('--scenes', 'cases,runs').split(',');
const wait = (p, n) => p.waitForTimeout(n);
async function click(p, locator) {
  await locator.scrollIntoViewIfNeeded(); const b = await locator.boundingBox();
  if (!b) throw new Error('Target has no bounding box');
  await p.mouse.move(b.x+b.width/2, b.y+b.height/2, { steps: 30 });
  await wait(p, 180); await p.mouse.down(); await wait(p, 80); await p.mouse.up(); await wait(p, 650);
}
async function scroll(p, locator, offset=110) {
  const top = await locator.evaluate((el, off) => el.getBoundingClientRect().top + scrollY - off, offset);
  await p.evaluate(y => window.scrollTo({top:y, behavior:'smooth'}), top); await wait(p, 900);
}
function cursor() {
  const css=document.createElement('style'); css.textContent='html{scrollbar-width:none}::-webkit-scrollbar{display:none}#promo-cursor{position:fixed;top:0;left:0;pointer-events:none;z-index:2147483647;width:30px;height:30px;filter:drop-shadow(0 2px 3px #0008)}';document.head.append(css);
  const el=document.createElement('div');el.id='promo-cursor';el.innerHTML='<svg width="30" height="30" viewBox="0 0 30 30"><path d="M4 3V24L10 18L14 27L18 25L14 17L23 16Z" fill="white" stroke="#132033" stroke-width="1.5"/></svg>';document.body.append(el);
  const place=(x,y)=>el.style.transform=`translate(${x-4}px,${y-3}px)`;place(1500,750);
  addEventListener('mousemove',e=>place(e.clientX,e.clientY),true);
  addEventListener('mousedown',e=>{const r=document.createElement('div');Object.assign(r.style,{position:'fixed',left:e.clientX-15+'px',top:e.clientY-15+'px',width:'30px',height:'30px',border:'2px solid #22d3ee',borderRadius:'50%',zIndex:2147483646,pointerEvents:'none'});document.body.append(r);r.animate([{transform:'scale(.5)',opacity:1},{transform:'scale(2.8)',opacity:0}],{duration:550}).onfinish=()=>r.remove();},true);
}
(async()=>{
  fs.mkdirSync(path.join(out,'raw-v3'),{recursive:true});
  const browser = await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
  const manifest=fs.existsSync(path.join(out,'recordings.json'))?JSON.parse(fs.readFileSync(path.join(out,'recordings.json'),'utf8')):{};
  for(const name of sceneNames){
    const ctx=await browser.newContext({viewport:{width:1920,height:1080},recordVideo:{dir:path.join(out,'raw-v3'),size:{width:1920,height:1080}},locale:'zh-CN',timezoneId:'Asia/Shanghai'});
    const started=Date.now();const p=await ctx.newPage();const marks={};const mark=k=>marks[k]=(Date.now()-started)/1000;
    await p.goto(base+(name==='cases'?'#chan_cases':name==='model'?'#daily_predict':'#runs'),{waitUntil:'networkidle'});
    await p.evaluate(cursor);
    if(name==='cases'){
      await p.locator('#cc-event').selectOption('2014-07-31');
      if(await p.locator('#cc-future').isChecked()) await click(p,p.locator('#cc-future'));
      if(!(await p.locator('#cc-segments').isChecked())) await click(p,p.locator('#cc-segments'));
      await wait(p,600);mark('ready');
      await click(p,p.locator('#cc-next'));await wait(p,700);await click(p,p.locator('#cc-next'));await wait(p,700);
      const b=await p.locator('#cc-day').boundingBox();
      const value=Number(await p.locator('#cc-day').inputValue());const max=Number(await p.locator('#cc-day').getAttribute('max'));
      await p.mouse.move(b.x+b.width*value/max,b.y+b.height/2,{steps:20});await p.mouse.down();
      await p.mouse.move(b.x+b.width*.35,b.y+b.height/2,{steps:35});await p.mouse.up();await wait(p,2000);mark('end');
    } else if(name==='model') {
      await p.locator('#candidate-output').waitFor();
      await scroll(p,p.locator('.daily-model'),110);await wait(p,500);mark('ready');
      for(const selector of ['#candidate-output','.daily-model .kpi-grid']) {
        const box=await p.locator(selector).first().boundingBox();
        await p.mouse.move(box.x+box.width*.68,box.y+box.height*.45,{steps:35});await wait(p,1600);
      }
      await p.mouse.wheel(0,110);await wait(p,2400);mark('end');
    } else {
      await p.locator('.run-card').first().waitFor();await wait(p,400);mark('ready');
      const card=p.locator('.run-card').filter({hasText:'composite_factor'}).first();await click(p,card);
      await p.locator('#run-detail pre').waitFor({state:'visible',timeout:15000});mark('loaded');
      await scroll(p,p.locator('#run-detail'),270);mark('detail');await wait(p,2400);
      await scroll(p,p.locator('#artifacts-list'),220);await p.mouse.move(1560,350,{steps:30});await wait(p,2000);mark('end');
    }
    const video=p.video();await ctx.close();const filename=await video.path();manifest[name]={file:path.relative(out,filename),marks};console.log(name,marks);
  }
  await browser.close();fs.writeFileSync(path.join(out,'recordings.json'),JSON.stringify(manifest,null,2));
})().catch(e=>{console.error(e);process.exit(1)});
