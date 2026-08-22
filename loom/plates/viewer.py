"""A self-contained page that cross-dissolves a plate chain, so a morph can be judged rather than guessed.

This is the whole reason plates are produced by editing: consecutive plates share composition, so a
straight opacity cross-fade looks like the world changing in place. If a transition looks like a cut
here, the edit instruction let the composition move -- tighten the preservation clause, don't reach for
a video model.
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
 #cap{padding:0 14px 14px;color:#8b949e;white-space:pre-wrap;min-height:3em}
 b{color:#e6edf3}
</style></head><body>
<div id="stage"><img id="a"><img id="b"></div>
<div id="bar">
  <button id="play">play</button>
  <input type="range" id="t" min="0" max="1000" value="0">
  <span id="lbl"></span>
  <label><input type="checkbox" id="loop" checked> loop</label>
  <label>secs <input type="number" id="dur" value="2.5" step="0.5" min="0.5" style="width:4em"></label>
</div>
<div id="cap"></div>
<script>
const CHAIN = __CHAIN__;
const a=document.getElementById('a'), b=document.getElementById('b'), t=document.getElementById('t');
const lbl=document.getElementById('lbl'), cap=document.getElementById('cap');
const N=CHAIN.length;
// Position p runs 0..N-1: the integer part picks the pair, the fraction is the dissolve.
function draw(p){
  const i=Math.min(Math.floor(p), N-2), f=p-i;
  a.src=CHAIN[i].file; b.src=CHAIN[i+1].file; b.style.opacity=f;
  lbl.textContent=(i+1)+' \\u2192 '+(i+2)+' / '+N;
  const s=CHAIN[i+1];
  cap.innerHTML='<b>'+(s.kind||'edit')+'</b>  '+(s.secs?s.secs.toFixed(1)+'s  ':'')+
                (s.model||'')+'\\n'+(s.prompt||'').replace(/</g,'&lt;');
}
t.oninput=()=>draw(t.value/1000*(N-1));
let raf=null, t0=null;
function stop(){ if(raf) cancelAnimationFrame(raf); raf=null; document.getElementById('play').textContent='play'; }
function step(ts){
  if(t0===null) t0=ts;
  const dur=parseFloat(document.getElementById('dur').value)*1000*(N-1);
  let p=(ts-t0)/dur;
  if(p>=1){ if(document.getElementById('loop').checked){ t0=ts; p=0; } else { p=1; stop(); } }
  t.value=p*1000; draw(p*(N-1));
  if(raf!==null) raf=requestAnimationFrame(step);
}
document.getElementById('play').onclick=()=>{
  if(raf){ stop(); return; }
  t0=null; document.getElementById('play').textContent='pause'; raf=requestAnimationFrame(step);
};
if(N>1) draw(0); else cap.textContent='only one plate -- add an edit to see a morph';
</script></body></html>"""


def write(chain: list[dict], out: Path) -> Path:
    """Render the viewer next to the plates. `chain` entries need at least `file`."""
    slim = [{"file": Path(c["file"]).name, "prompt": c.get("prompt", ""),
             "model": c.get("model", ""), "secs": c.get("secs"), "kind": c.get("kind", "edit")}
            for c in chain]
    page = out / "index.html"
    page.write_text(PAGE.replace("__CHAIN__", json.dumps(slim)), encoding="utf-8")
    return page
