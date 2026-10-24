#!/usr/bin/env python3
"""Reproducible 1080p editorial cut from real screen recordings.

Local media is supplied with --work-dir; it never belongs in the public repo.
Requires Python with Pillow, NumPy, and ffmpeg / ffprobe on PATH.
"""
import argparse, concurrent.futures, json, math, os, subprocess, wave
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import numpy as np

W,H,FPS=1920,1080,30
PAPER='#f1f0eb'; INK='#122329'; MUTED='#687572'; TEAL='#007c83'; DARK='#06141b'; WHITE='#eef4f1'

def run(cmd):
    result=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if result.returncode: raise RuntimeError(result.stderr[-6000:])
    return result.stdout

def duration(p):
    return float(run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(p)]))

def ff(args): return run(['ffmpeg','-y','-hide_banner','-loglevel','error','-threads','2',*map(str,args)])

def font(size,english=False,bold=False):
    if english: return ImageFont.truetype(str(EN_BOLD if bold else EN_FONT),size)
    return ImageFont.truetype(str(ZH_FONT),size,index=ZH_INDEX)

def txt(d, xy, text, size=30, color=INK, english=False, bold=False):
    d.text(xy,text,font=font(size,english,bold),fill=color,anchor='lt')

def right(d,y,text,size,color,english=False):
    f=font(size,english);d.text((1856,y),text,font=f,fill=color,anchor='rt')

def center(d,y,text,size,color):
    d.text((960,y),text,font=font(size),fill=color,anchor='mt')

def brand(d,x,y,color=INK):
    d.arc((x,y,x+30,y+30),25,335,fill=TEAL,width=3)
    d.line([(x+19,y+20),(x+32,y+32)],fill=TEAL,width=3)
    txt(d,(x+45,y+1),'QuantWeave',28,color,True,True)

def frame(scene,n,out):
    dark=scene['id']=='opening';bg=DARK if dark else PAPER;fg=WHITE if dark else INK
    im=Image.new('RGB',(W,H),bg);d=ImageDraw.Draw(im)
    txt(d,(64,42),scene['chapter'],23,('#73cbd0' if dark else TEAL),True,True)
    brand(d,1600,34,fg)
    txt(d,(64,98),scene['title'],66 if dark else 58,fg)
    txt(d,(66,181),scene['tag'],26,('#a2b9bf' if dark else MUTED))
    d.rounded_rectangle((61,235,1859,976),radius=5,fill='#1e3d44' if dark else '#cdd2cd')
    d.rectangle((64,238,1855,973),fill='#020617')
    # Progress rail is an editorial chapter marker, not a UI performance meter.
    for i in range(8):
        x=1240+i*30;d.line((x,54,x+21,54),fill=(TEAL if i<=max(0,n-2) else '#c3ccc7'),width=3)
    path=out/f"{scene['id']}-frame.png";im.save(path);return path

def caption(text,path,dark=False):
    im=Image.new('RGBA',(W,H),(0,0,0,0));d=ImageDraw.Draw(im)
    center(d,1008,text,36,WHITE if dark else INK);im.save(path)

def outro_layers(out):
    bg=Image.new('RGB',(W,H),DARK);d=ImageDraw.Draw(bg)
    for i in range(12):
        x=1200+i*65;d.line((x,0,x-450,1080),fill='#0c2630',width=1)
    d.line((120,880,1800,880),fill='#24414a',width=1)
    txt(d,(120,76),'OPEN SOURCE / MIT',24,'#6ccad0',True,True)
    txt(d,(120,925),'A-SHARE RESEARCH WORKBENCH',22,'#8ea5ac',True)
    right(d,925,'研究用途 · 演示使用冻结快照与模拟账户',23,'#8ea5ac')
    bg.save(out/'outro-bg.png')
    layer=Image.new('RGBA',(W,H));d=ImageDraw.Draw(layer)
    brand(d,124,244,WHITE)
    txt(d,(116,312),'QuantWeave',140,WHITE,True,True)
    txt(d,(124,497),'每一步判断，都有来处。',64,'#69d9d9')
    txt(d,(126,635),'github.com/DWJLULALA/QuantWeave',35,WHITE,True)
    for i,t in enumerate(['数据','因子','结构','模型','模拟','记录']):
        x=126+i*280;d.ellipse((x,786,x+9,795),fill='#69d9d9');txt(d,(x+24,777),t,26,'#a4b8bc')
        if i<5:d.line((x+105,791,x+247,791),fill='#31525c',width=1)
    layer.save(out/'outro-type.png')

def synth_music(path,total):
    """Deterministic original sparse instrumental: warm pad, bass, plucks."""
    sr=48000;n=int((total+1)*sr);t=np.arange(n,dtype=np.float64)/sr
    stereo=np.zeros((n,2),dtype=np.float32);beat=60/100;bar=beat*4
    chords=[(45,52,60,64),(41,48,60,65),(48,55,60,64),(43,50,59,62)]
    def add(start,seconds,signal,pan=0):
        a=int(start*sr)
        if a>=n:return
        b=min(n,a+len(signal));signal=signal[:b-a]
        stereo[a:b,0]+=signal*np.sqrt((1-pan)/2);stereo[a:b,1]+=signal*np.sqrt((1+pan)/2)
    def hz(m):return 440*2**((m-69)/12)
    for b in range(math.ceil(total/bar)):
        notes=chords[(b//2)%4];start=b*bar
        ts=np.arange(int(bar*sr))/sr;en=np.sin(np.pi*np.clip(ts/bar,0,1))**1.4
        pad=sum(np.sin(2*np.pi*hz(m)*ts)+.15*np.sin(2*np.pi*hz(m)*2*ts) for m in notes[1:])*.011*en
        add(start,bar,pad,-.25);add(start+.015,bar,pad,.25)
        for step in [0,1,2.5,3.5]:
            st=np.arange(int(.42*sr))/sr;note=notes[(int(step*2)+b)%3+1]+12
            pluck=(np.sin(2*np.pi*hz(note)*st)+.13*np.sin(2*np.pi*hz(note)*3*st))*np.exp(-st*12)*(1-np.exp(-st*180))*.017
            add(start+step*beat,.42,pluck,(-.45 if step%2 else .45))
        for step in [0,2]:
            st=np.arange(int(.30*sr))/sr
            kick=np.sin(2*np.pi*(46*st+38*.025*(1-np.exp(-st/.025))))*np.exp(-st*22)*.023
            add(start+step*beat,.3,kick)
    fade=np.minimum(np.clip(t/1.6,0,1),np.clip((total-t)/2.5,0,1)).astype(np.float32)
    stereo*=fade[:,None]
    with wave.open(str(path),'wb') as f:
        f.setnchannels(2);f.setsampwidth(2);f.setframerate(sr);f.writeframes((np.clip(stereo,-1,1)*32767).astype('<i2').tobytes())

def build(a):
    root=a.work_dir; assets=root/'graphics'; clips=root/'rendered'; sources=root/'source-v2/qv2_clips'
    assets.mkdir(parents=True,exist_ok=True);clips.mkdir(parents=True,exist_ok=True)
    spec=json.loads(a.storyboard.read_text());scenes=spec['scenes']; timeline=[];global_start=0
    for scene in scenes:
        sid=scene['id'];voices=[];local=.22
        for i,line in enumerate(scene['lines']):
            wav=root/'audio'/f'{sid}-{i}.wav';dur=duration(wav)
            voices.append({'file':str(wav.relative_to(root)),'at':local,'duration':dur,'text':line})
            local+=dur+.12
        T=math.ceil((local+.30)*FPS)/FPS
        if scene['shots']: T=max(T,math.ceil(sum(s['end']-s['start'] for s in scene['shots'])*.84*FPS)/FPS)
        if sid=='outro':T=max(T,6.8)
        timeline.append({**scene,'duration':T,'start':global_start,'voices':voices});global_start+=T
    (root/'timeline.json').write_text(json.dumps({'duration':global_start,'scenes':timeline},ensure_ascii=False,indent=2))
    (root/'storyboard.json').write_text(a.storyboard.read_text())
    outro_layers(assets)

    def scene_video(pair):
        n,s=pair;sid=s['id'];T=s['duration'];segments=[]
        if a.only and sid not in a.only.split(','):return
        if sid!='outro':
            rawdur=sum(z['end']-z['start'] for z in s['shots']);factor=T/rawdur
            for k,z in enumerate(s['shots']):
                src=root/z['file'] if 'file' in z else sources/f"{z['source']}.mp4"
                dest=clips/f'{sid}-{k}.mp4';x,y,w,h=z['crop'];length=z['end']-z['start'];wanted=z.get('duration',length*factor);shot_factor=wanted/length
                vf=f'crop={w}:{h}:{x}:{y},setpts={shot_factor}*(PTS-STARTPTS),fps={FPS},scale=1792:736:flags=lanczos,setsar=1,tpad=stop_mode=clone:stop_duration=0.12,trim=duration={wanted},format=yuv420p'
                ff(['-ss',z['start'],'-t',length,'-i',src,'-vf',vf,'-an','-c:v','libx264','-preset','fast','-crf','18','-threads','2',dest]);segments.append(dest)
            inputs=[];filters=[]
            bg=frame(s,n,assets);inputs+=['-loop','1','-framerate','30','-i',bg]
            for path in segments: inputs+=['-i',path]
            count=len(segments)
            if count>1: filters.append(''.join(f'[{i+1}:v]' for i in range(count))+f'concat=n={count}:v=1:a=0[screen]')
            else: filters.append('[1:v]null[screen]')
            filters+=['[screen]tpad=stop_mode=clone:stop_duration=1[scpad]', '[0:v][scpad]overlay=64:238:shortest=1[base]']
            previous='base';next_input=count+1
        else:
            inputs=['-loop','1','-framerate','30','-i',assets/'outro-bg.png','-loop','1','-framerate','30','-i',assets/'outro-type.png']
            filters=['[1:v]format=rgba,fade=t=in:st=0:d=0.45:alpha=1[type]','[0:v][type]overlay=x=0:y=0[base]'];previous='base';next_input=2
        for i,v in enumerate(s['voices']):
            p=assets/f'{sid}-caption-{i}.png';caption(v['text'],p,sid in ['opening','outro']);inputs+=['-loop','1','-framerate','30','-i',p]
            st=v['at']+.10;en=v['at']+v['duration']
            filters.append(f'[{previous}][{next_input}:v]overlay=0:0:enable=\'between(t,{st},{en})\'[c{i}]');previous=f'c{i}';next_input+=1
        # Clear sample label independent of the application's tiny original badge.
        label=Image.new('RGBA',(W,H));d=ImageDraw.Draw(label)
        txt(d,(1520,184),'DEMO · 冻结样例',21,'#739293' if sid in ['opening','outro'] else MUTED)
        if sid not in ['outro']: label.save(assets/f'{sid}-label.png');inputs+=['-loop','1','-framerate','30','-i',assets/f'{sid}-label.png'];filters.append(f'[{previous}][{next_input}:v]overlay=0:0[label]');previous='label'
        filters.append(f'[{previous}]format=yuv420p[v]')
        dest=clips/f'{sid}.mp4'
        ff([*inputs,'-filter_complex_threads','1','-filter_complex',';'.join(filters),'-map','[v]','-an','-t',T,'-r','30','-c:v','libx264','-preset','fast','-crf','18','-threads','2','-pix_fmt','yuv420p','-colorspace','bt709','-color_primaries','bt709','-color_trc','bt709',dest])
        print(f"Rendered {sid}: {T:.2f}s",flush=True)
    if not a.assemble_only:
        with concurrent.futures.ThreadPoolExecutor(max_workers=a.jobs) as pool:
            list(pool.map(scene_video,enumerate(timeline)))
    if a.only:return
    concat=clips/'concat.txt';concat.write_text('\n'.join("file '"+s['id']+".mp4'" for s in timeline)+'\n')
    ff(['-f','concat','-safe','0','-i',concat,'-c','copy',root/'picture.mp4'])
    music=root/'music-v3.wav';synth_music(music,global_start)
    inputs=['-i',root/'picture.mp4','-i',music];filters=['[1:a]volume=0.8[music]'];tracks=['[music]'];idx=2
    subtitle=[]
    for s in timeline:
        for v in s['voices']:
            inputs+=['-i',root/v['file']];delay=round((s['start']+v['at'])*1000)
            filters.append(f'[{idx}:a]adelay={delay}|{delay}[a{idx}]');tracks.append(f'[a{idx}]');idx+=1
            subtitle.append((s['start']+v['at']+.1,s['start']+v['at']+v['duration'],v['text']))
    filters.append(''.join(tracks)+f'amix=inputs={len(tracks)}:duration=longest:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=8,aresample=48000,atrim=duration={global_start}[audio]')
    destination=root/'QuantWeave-v3.mp4'
    ff([*inputs,'-filter_complex_threads','1','-filter_complex',';'.join(filters),'-map','0:v','-map','[audio]','-c:v','copy','-c:a','aac','-b:a','192k','-ar','48000','-ac','2','-t',global_start,'-movflags','+faststart',destination])
    def tc(t):
        ms=round(t*1000);h,ms=divmod(ms,3600000);m,ms=divmod(ms,60000);s,ms=divmod(ms,1000)
        return f'{h:02}:{m:02}:{s:02},{ms:03}'
    (root/'QuantWeave-v3.srt').write_text('\n\n'.join(f'{i+1}\n{tc(st)} --> {tc(en)}\n{text}' for i,(st,en,text) in enumerate(subtitle))+'\n')
    print(f'Final: {destination} ({global_start:.2f}s)',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--work-dir',type=Path,required=True);p.add_argument('--storyboard',type=Path,default=Path(__file__).with_name('storyboard.json'));p.add_argument('--only');p.add_argument('--assemble-only',action='store_true');p.add_argument('--jobs',type=int,default=3)
    p.add_argument('--zh-font',type=Path,default=Path('/System/Library/Fonts/STHeiti Medium.ttc'));p.add_argument('--zh-index',type=int,default=1)
    p.add_argument('--en-font',type=Path,default=Path('/System/Library/Fonts/Supplemental/Arial.ttf'));p.add_argument('--en-bold',type=Path,default=Path('/System/Library/Fonts/Supplemental/Arial Bold.ttf'))
    args=p.parse_args();ZH_FONT=args.zh_font;ZH_INDEX=args.zh_index;EN_FONT=args.en_font;EN_BOLD=args.en_bold;build(args)
