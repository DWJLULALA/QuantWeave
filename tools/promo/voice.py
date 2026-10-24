#!/usr/bin/env python3
"""Generate narration and sentence timing. Requires edge-tts and ffmpeg."""
import argparse, asyncio, json, subprocess
from pathlib import Path
import edge_tts

async def main():
    p=argparse.ArgumentParser(); p.add_argument('--work-dir',type=Path,required=True); p.add_argument('--storyboard',type=Path,default=Path(__file__).with_name('storyboard.json'))
    a=p.parse_args(); spec=json.loads(a.storyboard.read_text()); out=a.work_dir/'audio'; out.mkdir(parents=True,exist_ok=True)
    sem=asyncio.Semaphore(3)
    async def generate(scene):
        async with sem:
            for n,line in enumerate(scene['lines']):
                key=f"{scene['id']}-{n}"; mp3=out/f'{key}.mp3'; timing=out/f'{key}.json'; meta=out/f'{key}.voice.json'
                config={k:spec[k] for k in ('voice','rate','pitch')};config['text']=line
                if mp3.exists() and timing.exists() and meta.exists() and json.loads(meta.read_text())==config:
                    if not (out/f'{key}.wav').exists():
                        subprocess.run(['ffmpeg','-y','-loglevel','error','-i',str(mp3),'-ar','48000','-ac','1',str(out/f'{key}.wav')],check=True)
                    continue
                for attempt in range(3):
                    try:
                        events=[]
                        talk=edge_tts.Communicate(line,spec['voice'],rate=spec['rate'],pitch=spec['pitch'],boundary='SentenceBoundary')
                        with mp3.open('wb') as f:
                            async for event in talk.stream():
                                if event['type']=='audio': f.write(event['data'])
                                elif event['type']=='SentenceBoundary': events.append(event)
                        timing.write_text(json.dumps(events,ensure_ascii=False,indent=2));meta.write_text(json.dumps(config,ensure_ascii=False,indent=2));break
                    except Exception:
                        if attempt==2: raise
                        await asyncio.sleep(1+attempt)
                subprocess.run(['ffmpeg','-y','-loglevel','error','-i',str(mp3),'-ar','48000','-ac','1',str(out/f'{key}.wav')],check=True)
                print(key,flush=True)
    await asyncio.gather(*(generate(s) for s in spec['scenes']))

if __name__=='__main__': asyncio.run(main())
