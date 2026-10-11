"""Build a local evidence viewer. Serve its output directory on loopback only."""
import argparse
import json
from pathlib import Path

PAGE = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOSSIP 语音语义 Demo</title>
<style>
:root{--ink:#1f2936;--muted:#647283;--line:#dae1e6;--blue:#3157bd;--paper:#f5f7f9;--good:#126b51;--bad:#a34031}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1240px;margin:auto;padding:44px 32px 80px}header{display:flex;justify-content:space-between;align-items:flex-start;gap:24px}.eyebrow{color:var(--blue);font-size:12px;letter-spacing:2px;font-weight:700}h1{font-size:34px;letter-spacing:-1px;margin:10px 0 12px;line-height:1.3}p{margin:8px 0}h2{font-size:19px;margin:32px 0 12px}.muted{color:var(--muted)}.scope{max-width:860px}.button{border:1px solid var(--line);border-radius:8px;padding:9px 15px;background:white;cursor:pointer;color:var(--ink);font:inherit;white-space:nowrap}.button:hover{border-color:var(--blue)}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:28px 0}.metric{padding:19px 21px;background:white;border:1px solid var(--line);border-radius:12px}.metric b{display:block;font-size:29px;line-height:1.4}.metric span{font-size:12px;color:var(--muted)}.note{background:#eaf0fe;padding:16px 20px;border-radius:10px;font-size:14px}.filters{display:flex;gap:12px;flex-wrap:wrap;padding:15px 0;position:sticky;top:0;background:var(--paper);z-index:2;border-bottom:1px solid var(--line);margin-bottom:16px}select,input{font:inherit;padding:9px 12px;border:1px solid var(--line);border-radius:7px;background:white;color:var(--ink)}input{min-width:260px;flex:1}.counter{align-self:center;color:var(--muted);font-size:13px}details.run{background:white;border:1px solid var(--line);border-radius:10px;margin:10px 0;overflow:hidden}summary{padding:17px 20px;cursor:pointer;display:flex;gap:12px;align-items:center;list-style:none}summary::before{content:"＋";color:var(--muted)}details[open]>summary::before{content:"−"}summary .title{font-weight:650;flex:1}.pill{font-size:11px;padding:4px 9px;border-radius:20px;white-space:nowrap;background:#eef1f5}.good{color:var(--good);background:#e8f5ee}.bad{color:var(--bad);background:#fff0eb}.pending{color:#815b00;background:#fff6d9}.detail{border-top:1px solid var(--line);padding:20px}.cols{display:grid;grid-template-columns:1fr 1fr;gap:24px}.label{font-size:11px;font-weight:700;color:var(--muted);letter-spacing:1px;margin-bottom:7px}.bubble{padding:13px 16px;border-radius:9px;background:#f2f5f9;margin:8px 0;white-space:pre-wrap;overflow-wrap:anywhere}.answer{background:#eef3ff}.history{font-size:13px;color:var(--muted);margin:8px 0}pre{font:12px/1.6 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7f9;padding:12px;border-radius:7px;max-height:380px;overflow:auto}audio{width:100%;height:34px;margin:8px 0}a{color:var(--blue)}.step{padding-top:15px;border-top:1px solid var(--line);margin-top:15px}.issue{color:var(--bad);padding:8px 0}.review{padding:10px 13px;background:#fff7e9;border-radius:6px;margin:12px 0;font-size:13px}.empty{padding:40px;text-align:center;color:var(--muted)}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:12px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted)}.foot{margin-top:28px;font-size:12px;color:var(--muted)}@media(max-width:720px){main{padding:25px 16px}header{display:block}h1{font-size:27px}.metrics{grid-template-columns:repeat(2,1fr)}.cols{grid-template-columns:1fr}summary{flex-wrap:wrap;padding:13px}.title{min-width:70%}input{min-width:100%}.pill{font-size:10px}.filters{position:static}}
</style>
<main><header><div><div class="eyebrow">BOSSIP / VOICE LAB</div><h1>让模型理解，再让工具执行</h1><p class="scope muted">真实调用 Audio 3.1 与 Omni 3.8，验证自然确认、选择、改口、语气调整和业务结果播报。展开场景可以查看工具参数、完整回复并试听。</p></div><button class="button" id="refresh">刷新结果</button></header>
<div class="metrics" id="metrics"></div>
<div class="note">测试边界：工具连接内存中的模拟业务；语音输入由本机 Tingting 合成。旧规则对照是原函数的隔离校验，未运行完整旧版应用。结构校验通过只代表工具选择与参数符合预期；回复事实准确性单独审核，音色与自然度需要试听。</div>
<section id="findings"></section>
<h2>逐场景证据</h2><div class="filters"><select id="model" aria-label="模型"><option value="">全部模型</option><option value="audio">Audio 3.1</option><option value="omni">Omni 3.8</option></select><select id="mode" aria-label="输入方式"><option value="">全部输入</option><option value="text">文字输入</option><option value="audio">语音输入</option></select><select id="status" aria-label="结果筛选"><option value="">全部结果</option><option value="issue">发现问题</option><option value="pass">结构通过</option></select><input id="search" aria-label="搜索场景" placeholder="搜索场景、输入或工具…"><span class="counter" id="count"></span></div><div id="runs"></div>
<h2>原有词法规则对照</h2><p class="muted">这些结果来自当前工作区真实匹配函数。新的工具执行器不读取用户原文进行关键词分类。</p><div id="legacy"></div>
<h2>工具执行校验</h2><div id="checks"></div><p class="foot" id="foot"></p></main>
<script>
let data={runs:[]};const $=id=>document.getElementById(id);const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const audio=(path,label)=>path?`<div class="label">${esc(label)}</div><audio controls preload="none" src="${esc(path)}"></audio>`:'';
const issue=r=>!r.pass||(r.review&&r.review.pass===false);const model=r=>r.model.includes('audio-3')?'audio':'omni';
function render(){const rows=data.runs.filter(r=>(!$('model').value||model(r)===$('model').value)&&(!$('mode').value||r.mode===$('mode').value)&&(!$('status').value||($('status').value==='issue'?issue(r):r.pass))&&JSON.stringify(r).toLowerCase().includes($('search').value.toLowerCase()));$('count').textContent=`${rows.length} 条记录`;
$('runs').innerHTML=rows.map(r=>{const name=model(r)==='audio'?'Audio 3.1':'Omni 3.8';const badge=issue(r)?'<span class="pill bad">发现问题</span>':'<span class="pill good">结构通过</span>';const review=r.review?`<div class="review">回复复核：${esc(r.review.note)}</div>`:'<div class="review">结构校验已完成；对回复事实准确性和主观听感需另行复核。</div>';return `<details class="run"><summary><span class="title">${esc(r.title)}</span><span class="pill">${name}</span><span class="pill">${r.mode==='audio'?'语音输入':'文字输入'}</span><span class="pill">${esc(r.batch)}</span>${badge}</summary><div class="detail">${review}${r.error?`<div class="issue">${esc(r.error_type)}：${esc(r.error)}</div>`:''}${(r.history||[]).length?`<details><summary>查看前文</summary>${r.history.map(x=>`<div class="history">${x[0]==='user'?'用户':'助理'}：${esc(x[1])}</div>`).join('')}</details>`:''}${(r.steps||[]).map((s,i)=>`<div class="step"><div class="cols"><div><div class="label">输入 · 第 ${i+1} 轮</div><div class="bubble">${esc(s.input||JSON.stringify(s.background))}</div>${audio(s.input_audio,'合成用户语音')}<div class="label">实际工具调用</div>${s.calls.length?`<pre>${esc(JSON.stringify(s.calls.map(c=>({tool:c.name,arguments:c.arguments,result:c.output})),null,2))}</pre>`:'<p class="muted">没有调用业务工具</p>'}<details><summary>查看预设期望与检查结果</summary><pre>${esc(JSON.stringify({expected:s.expectation,grade:s.grade},null,2))}</pre></details></div><div><div class="label">模型实际回复</div>${s.responses.map(q=>`${q.text?`<div class="bubble answer">${esc(q.text)}</div>`:''}${audio(q.audio_file,'模型原声')}${(q.input_transcripts||[]).length?`<div class="history">识别文本：${esc(q.input_transcripts.join(' / '))}</div>`:''}`).join('')}</div></div></div>`).join('')}${r.trace?`<p><a target="_blank" href="${esc(r.trace)}">查看原始事件日志</a></p>`:''}</div></details>`}).join('')||'<div class="empty">没有符合筛选条件的记录</div>'}
async function load(){data=await(await fetch('report.json?'+Date.now())).json();const reviewed=data.runs.filter(r=>r.review);$('metrics').innerHTML=[[data.runs.length,'真实模型场景记录'],[data.runs.filter(r=>r.pass).length,'工具与参数结构通过'],[data.runs.filter(issue).length,'需处理或复核的问题'],[data.runs.filter(r=>r.mode==='audio').length,'语音输入场景记录']].map(([n,t])=>`<div class="metric"><b>${n}</b><span>${t}</span></div>`).join('');$('findings').innerHTML=data.findings?'<h2>这次验证发现了什么</h2>'+data.findings.map(x=>`<p>• ${esc(x)}</p>`).join(''):'';$('legacy').innerHTML='<table><thead><tr><th>用户表达</th><th>原规则</th><th>结果</th></tr></thead><tbody>'+data.legacy.map(x=>`<tr><td>${esc(x.input)}</td><td>${esc(x.rule)}</td><td>${esc(JSON.stringify(x.actual))}</td></tr>`).join('')+'</tbody></table>';$('checks').innerHTML=data.sandbox_checks.map(c=>`<span class="pill ${c.pass?'good':'bad'}">${esc(c.name)} · ${c.pass?'通过':'未通过'}</span> `).join('');$('foot').textContent=`${data.generated_at} · ${reviewed.length} 条记录已进行回复文本复核。所有运行与失败记录均保留；不代表生产环境验收。`;render()}
for(const id of ['model','mode','status','search'])$(id).addEventListener('input',render);$('refresh').onclick=load;load().catch(e=>$('runs').textContent='无法读取结果：'+e.message);
</script></html>'''

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("batches", nargs="+", help="Directories relative to root")
    args = parser.parse_args()
    from datetime import datetime
    from zoneinfo import ZoneInfo
    result = {"runs": [], "legacy": [], "sandbox_checks": [], "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()}
    reviews_path = args.root / "reviews.json"
    reviews = json.loads(reviews_path.read_text()) if reviews_path.exists() else {}
    result["findings"] = reviews.get("findings", [])
    for batch in args.batches:
        source = json.loads((args.root / batch / "results.json").read_text())
        if not result["legacy"]:
            result["legacy"] = source["legacy"]
        checks = {c["name"]: c for c in result["sandbox_checks"]}
        checks.update({c["name"]: c for c in source["sandbox_checks"]})
        result["sandbox_checks"] = list(checks.values())
        for row in source["runs"]:
            row["batch"] = batch
            row["review"] = reviews.get("runs", {}).get("/".join([batch, row["model"], row["mode"], row["id"]]))
            if row.get("trace"):
                row["trace"] = batch + "/" + row["trace"]
            for step in row.get("steps", []):
                if step.get("input_audio"):
                    step["input_audio"] = batch + "/" + step["input_audio"]
                for response in step["responses"]:
                    if response.get("audio_file"):
                        response["audio_file"] = batch + "/" + response["audio_file"]
            result["runs"].append(row)
    page = PAGE.replace('<input id="search"', '<select id="batch" aria-label="实验轮次"><option value="">全部轮次</option>'
        '<option value="main">初版</option><option value="refined">工具契约调整</option>'
        '<option value="continuous">连续音频复测</option></select><input id="search"')
    page = page.replace("const rows=data.runs.filter(r=>", "const rows=data.runs.filter(r=>(!$('batch').value||r.batch===$('batch').value)&&")
    page = page.replace("['model','mode','status','search']", "['model','mode','status','batch','search']")
    (args.root / "index.html").write_text(page)
    (args.root / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"Built viewer with {len(result['runs'])} records: {args.root / 'index.html'}")

if __name__ == "__main__":
    main()
