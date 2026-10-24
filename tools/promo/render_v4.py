#!/usr/bin/env python3
"""Narrative promo: animated mechanisms, attributed evidence, real demo clips.

Raw media lives in --work-dir. Sources use the layout documented in README.md.
No generated stock prices are presented as a backtest or live prediction.
"""
import argparse
import concurrent.futures
from functools import lru_cache
import json
import math
from pathlib import Path
import subprocess
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from render import duration, ff, synth_music

FPS = 30
INK = '#071923'
PANEL = '#102b37'
LINE = '#23424e'
WHITE = '#f2f4ed'
MUTED = '#9aafb6'
CYAN = '#73e2cd'
RED = '#ff8275'
GOLD = '#f1c578'
PAPER = '#f2f1e9'


def ease(v):
    v = min(1, max(0, v))
    return 1 - (1-v)**3


@lru_cache(maxsize=120)
def font(size, en=False, bold=True):
    return ImageFont.truetype(str(EN if en else ZH), int(size), index=0 if en else (ZH_BOLD if bold else ZH_REGULAR))


def text(d, x, y, value, size=36, color=WHITE, en=False, anchor='lt', max_width=None, bold=True):
    f = font(size, en, bold)
    if max_width:
        while d.textlength(value, font=f) > max_width and size > 18:
            size -= 1
            f = font(size, en, bold)
    d.text((int(x), int(y)), value, font=f, fill=color, anchor=anchor)


def pill(d, box, label, fill=PANEL, fg=CYAN, size=28):
    d.rounded_rectangle(box, radius=18, fill=fill)
    x0,y0,x1,y1=box
    text(d,(x0+x1)/2,(y0+y1)/2,label,size,fg,anchor='mm',max_width=x1-x0-24)


def line_path(d, points, progress, color=CYAN, width=8):
    count=(len(points)-1)*min(1,max(0,progress))
    full=int(count)
    drawn=points[:full+1]
    if full < len(points)-1:
        a,b=points[full],points[full+1];r=count-full
        drawn=drawn+[(a[0]+(b[0]-a[0])*r,a[1]+(b[1]-a[1])*r)]
    if len(drawn)>1:d.line(drawn,fill=color,width=width,joint='curve')
    if drawn:
        x,y=drawn[-1];d.ellipse((x-8,y-8,x+8,y+8),fill=color)


def wrap_line(s, maximum):
    # Soft split at punctuation; rendered subtitles remain independently timed.
    result=[]
    while len(s)>maximum:
        cut=max((s.rfind(p,maximum//2,maximum+1)+1 for p in '，。；：、'),default=0)
        if cut <= maximum//2:
            cut=maximum
            if s[cut-1].isascii() and s[cut-1].isalnum() and s[cut].isascii() and s[cut].isalnum():
                while cut>maximum//2 and s[cut-1].isascii() and s[cut-1].isalnum():cut-=1
        result.append(s[:cut]);s=s[cut:]
    if s:result.append(s)
    return result


def subtitle(d, scene, t, portrait=False, light=False):
    for v in scene['voices']:
        if v['at'] <= t < v['at']+v['duration']+.08:
            lines=wrap_line(v['text'],23 if portrait else 38)
            if portrait:
                for j,line in enumerate(lines):text(d,500,1560+j*62,line,42,WHITE,anchor='mt',max_width=900)
            else:
                for j,line in enumerate(lines):text(d,960,974+j*48,line,38,INK if light else WHITE,anchor='mt',max_width=1780)
            break


def base(scene,t,portrait=False):
    w,h=(1080,1920) if portrait else (1920,1080)
    light=scene['id']=='benchmark'
    im=Image.new('RGB',(w,h),PAPER if light else INK);d=ImageDraw.Draw(im)
    if not light:
        for x in range(0,w,120):d.line((x,0,x,h),fill='#0b222d',width=1)
        for y in range(0,h,120):d.line((0,y,w,y),fill='#0b222d',width=1)
    text(d,70 if portrait else 80,90 if portrait else 50,'QuantWeave',34,CYAN if not light else '#007969',True)
    if portrait:
        text(d,70,146,'先看阶段，再看排名',28,MUTED)
    else:
        text(d,1840,56,'OPEN SOURCE  /  RESEARCH',24,MUTED if not light else '#697c7f',True,anchor='rt')
        if scene['id']!='hook':text(d,80,133,scene['title'],65,INK if light else WHITE,max_width=1760)
    return im,d


def frame(scene,t,portrait=False,video=None):
    im,d=base(scene,t,portrait);sid=scene['id'];p=ease(t/1.1)
    if portrait:
        if sid=='short_hook':
            text(d,70,370,'股票排得很准',100,max_width=920)
            text(d,70,550,'账户为什么',108,RED,max_width=920)
            text(d,70,710,'还在亏？',140,RED)
            pill(d,(70,1070,970,1170),'RankIC 高 ≠ 一定赚钱',size=45)
            text(d,70,1260,'量化里最容易忽略的一步',42,MUTED)
        elif sid=='short_rank':
            text(d,70,340,'第一名，',100)
            text(d,70,490,'也可能是负收益。',82,RED,max_width=920)
            for i,v in enumerate(['-1%','-3%','-5%']):
                y=730+i*150;pill(d,(70,y,970,y+120),f'{i+1}    {v}',fg=RED,size=66)
            text(d,70,1260,'排序全对 · 三只全跌',46,CYAN)
            text(d,70,1350,'机制示意，非回测',30,MUTED)
        elif sid=='short_stack':
            text(d,70,320,'我把研究',108)
            text(d,70,480,'拆成三层。',108,CYAN)
            cards=[('01','升浪 × 缠论','市场现在是什么阶段'),('02','Kronos + LoRA + EMA','适应近期数据，平滑排名'),('03','SET10','补充反转与震荡风格')]
            for i,(n,a,b) in enumerate(cards):
                y=700+i*230;d.rounded_rectangle((70,y,970,y+195),24,fill=PANEL)
                text(d,100,y+32,n,36,CYAN,True);text(d,185,y+30,a,49,max_width=735)
                text(d,185,y+108,b,34,MUTED,max_width=735)
            text(d,70,1440,'研究方法展示 · 不承诺收益',28,MUTED)
        else:
            text(d,70,320,'把安装',115)
            text(d,70,490,'交给你的 AI。',108,CYAN,max_width=930)
            text(d,70,710,'GitHub',40,MUTED,True)
            text(d,70,790,'DWJLULALA /',57,en=True)
            text(d,70,880,'QuantWeave',80,en=True)
            pill(d,(70,1040,970,1150),'README_AGENT.md',size=49)
            text(d,70,1230,'克隆 → 测试 → 启动本地 demo',40,max_width=920)
            text(d,70,1340,'无需 GPU · 无需行情账号',40,GOLD)
        subtitle(d,scene,t,True)
        d.rectangle((70,1780,70+900*min(1,t/scene['duration']),1785),fill=CYAN)
        return im

    if sid=='hook':
        text(d,80,183,'给做多策略的一道题',33,MUTED)
        text(d,80,310-int(20*(1-p)),'股票排得很准',117,max_width=1740)
        if t>.65:
            text(d,80,495,'账户为什么还在亏？',128,RED,max_width=1740)
        d.line((80,735,80+int(1760*ease((t-.7)/1.2)),735),fill=RED,width=5)
        text(d,80,803,'排名能力',40,MUTED);text(d,450,790,'≠',80,RED)
        text(d,620,803,'市场方向',40,MUTED)
        text(d,1840,820,'01 / 先看阶段',28,CYAN,anchor='rt')
    elif sid=='rank':
        text(d,80,235,'同样的正确排序，两种完全不同的市场。',35,MUTED)
        for k,(name,returns,col) in enumerate([('下跌环境',['-1%','-3%','-5%'],RED),('上涨环境',['+5%','+3%','+1%'],CYAN)]):
            x=80+k*900;d.rounded_rectangle((x,330,x+860,830),24,fill=PANEL)
            text(d,x+40,373,name,47,col)
            for i,r in enumerate(returns):
                y=480+i*92
                text(d,x+40,y,f'排名 {i+1}',36,MUTED)
                d.rounded_rectangle((x+260,y-5,x+260+max(1,int(abs(int(r[:-1]))*80*p)),y+58),10,fill='#193b47')
                text(d,x+760,y,r,58,col,True,anchor='rt')
            text(d,x+40,770,'两组 RankIC 都是 1.0',31,WHITE)
        text(d,80,889,'机制示意，非回测；上涨环境也不保证任何个股盈利。',30,MUTED)
    elif sid in ('phase','operate'):
        if video is not None:im.paste(video,(160,260));d=ImageDraw.Draw(im)
        if sid=='phase':
            text(d,82,218,'大盘 → 行业 → 个股   /   不同层次，不同时间尺度',28,CYAN)
        else:
            names=['参数','排名','回放','留痕'];i=min(3,int(t/scene['duration']*4))
            for n,label in enumerate(names):pill(d,(80+n*220,210,270+n*220,251),label,fg=CYAN if n==i else MUTED,size=25)
        text(d,1840,222,'DEMO · 冻结样例',25,MUTED,anchor='rt')
        d.rectangle((159,259,1760,918),outline=LINE,width=2)
        text(d,80,935,'真实界面操作录屏 · 等待过程已剪短',23,MUTED)
    elif sid=='wave':
        text(d,80,236,'推动与回撤交替，观察结构是否持续抬升。',35,MUTED)
        points=[(100,780),(285,600),(430,687),(650,467),(820,555),(1040,325),(1250,415)]
        for y in [350,500,650,800]:d.line((80,y,1300,y),fill=LINE,width=1)
        line_path(d,points,ease(t/4),CYAN,10)
        if t>1.5:
            for x,y,label in [(285,600,'高点'),(650,467,'更高的高点'),(820,555,'更高的低点')]:
                text(d,x,y-62,label,28,CYAN,anchor='mt')
        for i,(a,b) in enumerate([('结构','高点与低点'),('趋势','均线与动量'),('方向','上涨 / 下跌 / 盘整')]):
            y=335+i*178;d.rounded_rectangle((1390,y,1840,y+145),18,fill=PANEL)
            text(d,1420,y+25,a,31,MUTED);text(d,1420,y+79,b,34,max_width=390)
        text(d,80,888,'结构原理示意，非行情预测；规则倾向值不等于上涨概率。',30,MUTED)
    elif sid=='chan':
        labels=['K 线与分型','笔 / 线段','重叠中枢']
        for i,a in enumerate(labels):pill(d,(80+i*600,255,630+i*600,325),a,size=35)
        # Deliberately schematic, with no ticker, dates or financial axis.
        for i in range(14):
            x=110+i*32;mid=620+100*math.sin(i*.65);body=30+12*(i%3);col=CYAN if i%3 else RED
            d.line((x,mid-body-30,x,mid+body+30),fill=col,width=3)
            d.rectangle((x-8,mid-body,x+8,mid+body),fill=col)
        pts=[(710,730),(800,470),(895,665),(1000,510),(1100,710),(1220,430)]
        line_path(d,pts,ease(t/3),CYAN,7)
        d.rounded_rectangle((1330,495,1785,685),16,fill='#4c4030',outline=GOLD,width=2)
        pts2=[(1310,780),(1400,420),(1490,685),(1600,495),(1700,740),(1830,370)]
        line_path(d,pts2,ease((t-.7)/3),GOLD,7)
        text(d,1555,575,'重叠区间',32,GOLD,anchor='mt')
        text(d,80,846,'日线 → 周线 → 月线',38,CYAN)
        text(d,1840,855,'结构简图 · 以确认时间为准',27,MUTED,anchor='rt')
    elif sid=='kronos':
        text(d,80,235,'近期适配 + 历史回放，窗口必须由验证决定。',34,MUTED)
        pill(d,(80,330,850,475),'近期训练窗口（例如近两个月）',size=39)
        pill(d,(890,330,1280,475),'标签隔离期',fill='#4b3c2b',fg=GOLD,size=40)
        pill(d,(1320,330,1840,475),'评测 / 交易区间',size=39)
        d.line((80,523,1840,523),fill=LINE,width=4)
        for x,label in [(850,'训练标签截止'),(1320,'评测边界')]:
            d.line((x,495,x,550),fill=GOLD,width=4);text(d,x,572,label,29,GOLD,anchor='mt')
        for i,(name,detail) in enumerate([('Kronos base','量价时序表征'),('LoRA 适配','少量参数更新'),('横截面排名','比较相对强弱')]):
            x=80+i*600;d.rounded_rectangle((x,675,x+550,840),22,fill=PANEL)
            text(d,x+35,708,name,46,CYAN,max_width=480);text(d,x+35,778,detail,30,MUTED)
        text(d,80,895,'时间轴为方法示意；rp10 对照采用七月增量 + 10% 历史回放。',30,MUTED)
    elif sid=='ema':
        text(d,80,236,'把预测变成相对排名，再处理日间噪声。',35,MUTED)
        xs=np.linspace(100,1380,36);raw=np.array([550+100*math.sin(i*.28)+85*math.sin(i*1.73) for i in range(36)])
        sm=[raw[0]]
        for v in raw[1:]:sm.append(.8*sm[-1]+.2*v)
        for y in [350,500,650,800]:d.line((80,y,1400,y),fill=LINE,width=1)
        line_path(d,list(zip(xs,raw)),ease(t/3),'#507180',4)
        line_path(d,list(zip(xs,sm)),ease((t-.2)/3),CYAN,9)
        text(d,1480,410,'原始排名',39,'#70939f');text(d,1480,515,'EMA 平滑',44,CYAN)
        text(d,1480,650,'更少跳动',35);text(d,1480,720,'也会滞后',35,RED)
        text(d,80,888,'平滑机制示意，非实验曲线；参数需结合预测池化与交易周期验证。',30,MUTED)
    elif sid=='benchmark':
        text(d,80,237,'2026.08.24 — 08.28  /  五日平均 RankIC',35,'#64757b')
        for i,(label,val,col) in enumerate([('BASE',.0021,'#8ca09e'),('rp10 全速微调方案',.0809,'#007c70')]):
            y=390+i*235;text(d,80,y,label,39,INK)
            d.rounded_rectangle((80,y+72,80+max(3,int(1400*val/.10*p)),y+144),8,fill=col)
            text(d,1810,y+60,f'{val:.4f}',94,col,True,anchor='rt')
        for i in range(6):
            x=80+280*i;d.line((x,796,x,808),fill='#a7b3b0',width=2)
            text(d,x,826,f'{i*.02:.2f}',24,'#64757b',True,anchor='mt')
        text(d,80,897,'同评估协议 · 笔记转录，未重算原始预测 · 短窗口，不代表长期表现',29,'#64757b')
    elif sid=='set10':
        second=scene['voices'][1]['at'];show=t>=second-.3
        if not show:
            text(d,80,238,'十因子等权 rank，补充另一种选股风格。',35,MUTED)
            names=['REV_OH','OVERNIGHT','ILLIQ_20','BETA_60','VOL_RATIO','D_MA20','POS_20','RET_5','RET_10','PV_CORR_20']
            for i,n in enumerate(names):
                x=80+(i%5)*356;y=360+(i//5)*170
                pill(d,(x,y,x+330,y+132),n,fg=CYAN if i<4 else GOLD,size=33)
            text(d,80,774,'Kronos：近期时序适配',43,CYAN)
            text(d,980,774,'SET10：反转 / 震荡风格',43,GOLD)
            text(d,80,892,'研究动机与因子定义；适用性仍需样本外检验。',30,MUTED)
        else:
            text(d,80,237,'2026.08.27  /  一个反转日的研究记录',35,MUTED)
            zero=1040;d.line((zero,350,zero,810),fill=MUTED,width=3)
            ep=ease((t-second+.3)/1.1)
            for i,(label,val,col) in enumerate([('Kronos rp10',-.362,CYAN),('SET10',.195,GOLD)]):
                y=415+i*225;text(d,80,y,label,46,col)
                end=zero+val*1800*ep;d.rectangle((min(end,zero),y+73,max(end,zero)+1,y+136),fill=col)
                text(d,1800,y+65,f'{val:+.3f}',79,col,True,anchor='rt')
            text(d,zero,828,'0',29,MUTED,True,anchor='mt')
            text(d,80,892,'指标为 RankIC；选择此日说明差异，完整八日摘要见证据页。',30,MUTED)
    elif sid=='data':
        for i,(a,b) in enumerate([('Tushare','个股日线 / 交易日历'),('东方财富','个股 30 分钟线'),('上交所 CSIP 接口','宽基指数日线'),('申万宏源研究','申万行业日线')]):
            x=80+(i%2)*900;y=295+(i//2)*215
            d.rounded_rectangle((x,y,x+860,y+180),22,fill=PANEL)
            text(d,x+35,y+30,a,48,CYAN);text(d,x+35,y+112,b,32,MUTED)
        text(d,80,800,'已收盘 → 质量校验 → 研究快照 → 日志与产物',43,max_width=1760)
        text(d,80,894,'真实研究环境的数据接入；demo 读取冻结快照，更新任务为模拟。',30,MUTED)
    elif sid=='cta':
        text(d,80,280,'把安装交给你的 AI。',104,CYAN,max_width=1760)
        pill(d,(80,460,1840,575),'github.com/DWJLULALA/QuantWeave',size=51,fg=WHITE)
        text(d,80,630,'README_AGENT.md',57,CYAN,True)
        text(d,1840,652,'克隆 → 测试 → 验证 → 启动',38,anchor='rt')
        d.line((80,758,1840,758),fill=LINE,width=2)
        text(d,80,807,'开源研究工作台',39)
        text(d,1840,807,'本地 Demo · 无需 GPU / 行情账号',39,GOLD,anchor='rt')
        text(d,80,905,'冻结样例用于体验流程；研究结果不构成收益承诺。',28,MUTED)
    subtitle(d,scene,t,False,sid=='benchmark')
    # A subtle global progress indicator, without UI-style performance claims.
    d.rectangle((80,1065,80+1760*min(1,t/scene['duration']),1069),fill='#007c70' if sid=='benchmark' else CYAN)
    return im


def prepare_video(scene,root,out):
    if not scene.get('shots'):return None
    srcs=[];raw_duration=sum(s['end']-s['start'] for s in scene['shots'])
    for i,s in enumerate(scene['shots']):
        source=root/s['file'] if 'file' in s else root/'source-v2/qv2_clips'/f"{s['source']}.mp4"
        target=out/f"{scene['id']}-shot{i}.mp4";length=s['end']-s['start'];factor=scene['duration']/raw_duration
        x,y,w,h=s['crop']
        ff(['-ss',s['start'],'-t',length,'-i',source,'-vf',f'crop={w}:{h}:{x}:{y},setpts={factor}*(PTS-STARTPTS),fps=30,scale=1600:658:flags=lanczos,setsar=1','-an','-c:v','libx264','-crf','18','-preset','fast','-threads','2',target]);srcs.append(target)
    listing=out/f"{scene['id']}-sources.txt";listing.write_text('\n'.join(f"file '{s.name}'" for s in srcs)+'\n')
    target=out/f"{scene['id']}-screen.mp4"
    ff(['-f','concat','-safe','0','-i',listing,'-vf','tpad=stop_mode=clone:stop_duration=1','-t',scene['duration'],'-an','-c:v','libx264','-crf','18','-preset','fast','-threads','2',target])
    return target


def build(args):
    root=args.work_dir;root.mkdir(parents=True,exist_ok=True)
    spec=json.loads(args.storyboard.read_text());portrait=args.portrait
    out=root/('short-rendered' if portrait else 'v4-rendered');out.mkdir(exist_ok=True)
    timeline=[];global_time=0
    for item in spec['scenes']:
        s=dict(item);local=.12;voices=[]
        for n,line in enumerate(s['lines']):
            path=root/'audio'/f"{s['id']}-{n}.wav";length=duration(path)
            voices.append(dict(file=str(path.relative_to(root)),at=local,duration=length,text=line));local+=length+.10
        total=math.ceil((local+.20)*FPS)/FPS
        s.update(duration=total,voices=voices,start=global_time);global_time+=total;timeline.append(s)
    name='QuantWeave-Douyin' if portrait else 'QuantWeave-v4'
    (root/f'{name}-timeline.json').write_text(json.dumps(dict(duration=global_time,scenes=timeline),ensure_ascii=False,indent=2))
    if args.preview:
        preview=root/('short-review' if portrait else 'review');preview.mkdir(exist_ok=True)
        for s in timeline:
            # Graphics preview only; actual UI is checked from encoded frames.
            t=s['duration']*.55;frame(s,t,portrait).save(preview/f"{s['id']}.jpg",quality=92)
        return

    def render_scene(s):
        if args.only and s['id'] not in args.only.split(','):return
        screen=prepare_video(s,root,out);decoder=None
        if screen:
            decoder=subprocess.Popen(['ffmpeg','-v','error','-threads','1','-i',str(screen),'-f','rawvideo','-pix_fmt','rgb24','-threads','1','pipe:1'],stdout=subprocess.PIPE)
        w,h=(1080,1920) if portrait else (1920,1080)
        dest=out/f"{s['id']}.mp4"
        log=(out/f"{s['id']}.log").open('w')
        encoder=subprocess.Popen(['ffmpeg','-y','-v','error','-f','rawvideo','-pix_fmt','rgb24','-s',f'{w}x{h}','-r','30','-i','pipe:0','-an','-c:v','libx264','-crf','19','-preset','fast','-threads','2','-pix_fmt','yuv420p','-colorspace','bt709','-color_primaries','bt709','-color_trc','bt709',str(dest)],stdin=subprocess.PIPE,stderr=log)
        previous=None
        try:
            for i in range(round(s['duration']*FPS)):
                if decoder:
                    data=decoder.stdout.read(1600*658*3)
                    if len(data)==1600*658*3:previous=Image.frombytes('RGB',(1600,658),data)
                im=frame(s,i/FPS,portrait,previous)
                encoder.stdin.write(im.tobytes())
        finally:
            encoder.stdin.close();code=encoder.wait();log.close()
            if decoder:decoder.stdout.close();decoder.terminate();decoder.wait()
        if code:raise RuntimeError(f"Encoder failed; see {s['id']}.log")
        print(f"Rendered {s['id']}: {s['duration']:.2f}s",flush=True)
    if not args.assemble_only:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:list(pool.map(render_scene,timeline))
    if args.only:return
    listing=out/'concat.txt';listing.write_text('\n'.join(f"file '{s['id']}.mp4'" for s in timeline)+'\n')
    picture=root/f'{name}-picture.mp4';ff(['-f','concat','-safe','0','-i',listing,'-c','copy',picture])
    music=root/f'{name}-music.wav';synth_music(music,global_time)
    inputs=['-i',picture,'-i',music];filters=['[1:a]volume=1.1[music]'];tracks=['[music]'];idx=2;subs=[]
    for s in timeline:
        for v in s['voices']:
            inputs+=['-i',root/v['file']];delay=round((s['start']+v['at'])*1000)
            filters.append(f'[{idx}:a]adelay={delay}|{delay}[a{idx}]');tracks.append(f'[a{idx}]');idx+=1
            subs.append((s['start']+v['at'],s['start']+v['at']+v['duration'],v['text']))
    filters.append(''.join(tracks)+f'amix=inputs={len(tracks)}:duration=longest:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=8,aresample=48000,atrim=duration={global_time}[audio]')
    ff([*inputs,'-filter_complex_threads','1','-filter_complex',';'.join(filters),'-map','0:v','-map','[audio]','-c:v','copy','-c:a','aac','-b:a','192k','-ar','48000','-ac','2','-t',global_time,'-movflags','+faststart',root/f'{name}.mp4'])
    def tc(t):
        ms=round(t*1000);h,ms=divmod(ms,3600000);m,ms=divmod(ms,60000);s,ms=divmod(ms,1000)
        return f'{h:02}:{m:02}:{s:02},{ms:03}'
    (root/f'{name}.srt').write_text('\n\n'.join(f'{i+1}\n{tc(st)} --> {tc(en)}\n{line}' for i,(st,en,line) in enumerate(subs))+'\n')
    print(f'Final {name}: {global_time:.2f}s',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--work-dir',type=Path,required=True)
    p.add_argument('--storyboard',type=Path,default=Path(__file__).with_name('storyboard-v4.json'))
    p.add_argument('--portrait',action='store_true');p.add_argument('--preview',action='store_true')
    p.add_argument('--only');p.add_argument('--assemble-only',action='store_true');p.add_argument('--jobs',type=int,default=2)
    p.add_argument('--zh-font',type=Path,default=Path('/System/Library/Fonts/Hiragino Sans GB.ttc'))
    p.add_argument('--zh-bold-index',type=int,default=2);p.add_argument('--zh-regular-index',type=int,default=0)
    p.add_argument('--en-font',type=Path,default=Path('/System/Library/Fonts/Supplemental/Arial Bold.ttf'))
    a=p.parse_args();ZH=a.zh_font;ZH_BOLD=a.zh_bold_index;ZH_REGULAR=a.zh_regular_index;EN=a.en_font;build(a)
