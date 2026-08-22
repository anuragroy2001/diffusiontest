"""A self-contained page that cross-dissolves a plate chain, so a morph can be judged rather than guessed.

This is the whole reason plates are produced by editing: consecutive plates share composition, so a
straight opacity cross-fade looks like the world changing in place. If a transition looks like a cut
here, the edit instruction let the composition move -- tighten the preservation clause, don't reach for
a video model.

The page re-reads `chain.json` on a timer, so a plate rendered while you are looking at it shows up on
its own and plays its transition. That needs the files served over HTTP, not opened as file:// -- use
`cli.py --serve`, which is the point of that flag.
"""

from __future__ import annotations

import json
from pathlib import Path

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Loom plates</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;background:#0b0d10;color:#c9d1d9;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
 #stage{position:relative;width:100%;aspect-ratio:16/9;background:#000;overflow:hidden}
 #stage img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
 #b{opacity:0}
 #bar{display:flex;gap:12px;align-items:center;padding:10px 14px;flex-wrap:wrap}
 input[type=range]{flex:1;min-width:220px}
 button{background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:6px 12px;
   font:inherit;cursor:pointer}
 button:hover{background:#30363d}
 #cap{padding:0 14px 14px;color:#8b949e;white-space:pre-wrap;min-height:3.2em}
 b{color:#e6edf3}
 #live{color:#3fb950}
 #live.off{color:#8b949e}
</style></head><body>
<div id="stage"><img id="a"><img id="b"></div>
<div id="bar">
  <button id="play">play</button>
  <input type="range" id="t" min="0" max="1000" value="0">
  <span id="lbl"></span>
  <label><input type="checkbox" id="loop"> loop</label>
  <label><input type="checkbox" id="follow" checked> follow</label>
  <label>secs <input type="number" id="dur" value="2.5" step="0.5" min="0.5" style="width:4em"></label>
  <span id="live">live</span>
</div>
<div id="cap"></div>
<script>
let CHAIN = __CHAIN__;
const a=document.getElementById('a'), b=document.getElementById('b'), t=document.getElementById('t');
const lbl=document.getElementById('lbl'), cap=document.getElementById('cap'), live=document.getElementById('live');
const base = f => String(f).split('/').pop();

function draw(p){
  const N=CHAIN.length;
  if(N===0){ cap.textContent='waiting for the first plate\\u2026'; return; }
  if(N===1){ a.src=base(CHAIN[0].file); b.style.opacity=0; lbl.textContent='1 / 1';
             cap.textContent='one plate \\u2014 add an edit to see a morph'; return; }
  const i=Math.max(0,Math.min(Math.floor(p), N-2)), f=p-i;
  a.src=base(CHAIN[i].file); b.src=base(CHAIN[i+1].file); b.style.opacity=f;
  lbl.textContent=(i+1)+' \\u2192 '+(i+2)+' / '+N;
  const s=CHAIN[i+1];
  cap.innerHTML='<b>'+(s.kind||'edit')+'</b>  '+(s.secs?s.secs.toFixed(1)+'s  ':'')+
                (s.model||'')+'\\n'+(s.prompt||'').replace(/</g,'&lt;');
}
// Position p runs 0..N-1: the integer part picks the pair, the fraction is the dissolve.
const span = () => Math.max(1, CHAIN.length-1);
let raf=null, t0=null, from=0, to=1;
function stop(){ if(raf) cancelAnimationFrame(raf); raf=null; document.getElementById('play').textContent='play'; }
function step(ts){
  if(t0===null) t0=ts;
  const per=parseFloat(document.getElementById('dur').value)*1000;
  let f=(ts-t0)/(per*Math.max(1,(to-from)));
  if(f>=1){
    if(document.getElementById('loop').checked){ t0=ts; f=0; }
    else { f=1; t.value=to/span()*1000; draw(to); stop(); return; }
  }
  const p=from+(to-from)*f;
  t.value=p/span()*1000; draw(p);
  if(raf!==null) raf=requestAnimationFrame(step);
}
function playFrom(x,y){ from=x; to=y; t0=null;
  document.getElementById('play').textContent='pause';
  if(raf) cancelAnimationFrame(raf);
  raf=requestAnimationFrame(step); }
t.oninput=()=>{ stop(); draw(t.value/1000*span()); };
document.getElementById('play').onclick=()=>{ if(raf){ stop(); return; } playFrom(0, span()); };

// Poll for new plates. A plate that lands while you are watching plays its own transition, which is
// what makes this usable as a live scratchpad rather than a report you regenerate by hand.
async function poll(){
  try{
    const r=await fetch('chain.json?_='+Date.now(), {cache:'no-store'});
    if(!r.ok) throw 0;
    const next=await r.json();
    live.textContent='live'; live.className='';
    if(next.length!==CHAIN.length){
      const grew=next.length>CHAIN.length, prev=CHAIN.length;
      CHAIN=next;
      if(grew && prev>=1 && document.getElementById('follow').checked) playFrom(prev-1, CHAIN.length-1);
      else if(!raf) draw(Math.min(t.value/1000*span(), span()));
    }
  }catch(e){ live.textContent='no server (open via cli.py --serve for live updates)'; live.className='off'; }
}
draw(0); poll(); setInterval(poll, 1500);
</script></body></html>"""


def write(chain: list[dict], out: Path) -> Path:
    """Render the viewer next to the plates. `chain` entries need at least `file`."""
    page = out / "index.html"
    page.write_text(PAGE.replace("__CHAIN__", json.dumps(slim(chain))), encoding="utf-8")
    return page


def slim(chain: list[dict]) -> list[dict]:
    """What the page needs: basenames, not the absolute paths the session records."""
    return [{"file": Path(c["file"]).name, "prompt": c.get("prompt", ""),
             "model": c.get("model", ""), "secs": c.get("secs"), "kind": c.get("kind", "edit")}
            for c in chain]
