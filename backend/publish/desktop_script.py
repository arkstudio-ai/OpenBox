"""The Playwright script that drives 抖音创作者中心's upload form on the desktop.

Runs on the desktop from `/opt/openbox/skills/dev-browser` (`npx tsx`), where
`connect()` returns the desktop Chrome carrying the person's login state (the
same runtime the dev-browser skill uses). Parameters are injected as JSON;
the script prints exactly one JSON line and never throws past its own
`main()`. Every step records what it saw so a failure names the step.

Form facts measured 2026-09-10 (spike C1/C1'): title `input[placeholder*=作品标题]`
(30 chars); intro is a contenteditable `.zone-container` that turns `#词 ` into a
topic chip; 自主声明 is a modal with `label.semi-radio` single-choice options
(the span inside intercepts clicks — click the label); 关联热点 is a
click-to-reveal field; 谁可以看 / 发布时间 are styled radios whose labels are
clickable text; the bottom bar has `发布` and `暂存离开`.
"""
from __future__ import annotations

import json

DECLARATIONS = {
    "ai": "内容由AI生成",
    "opinion": "内容为个人观点或见解",
    "repost": "内容为转载信息",
    "marketing": "内容含营销推广信息",
    "fiction": "虚构演绎，仅供娱乐",
    "none": "无需添加自主声明",
}
VISIBILITY = {"public": "公开", "friends": "好友可见", "private": "仅自己可见"}

UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
MANAGE_URL = "https://creator.douyin.com/creator-micro/content/manage"

SCRIPT = r'''import { connect, waitForPageLoad } from "@/client.js";
const P = __PARAMS__;
const out: any = { ok: false, step: "start", steps: [], risk: null, item_id: null, final_url: null, evidence: null };
const mark = (s: string, extra?: any) => { out.step = s; out.steps.push(extra === undefined ? s : { [s]: extra }); };
const bodyText = async (page: any) => (await page.evaluate(() => document.body.innerText)) as string;
const riskIn = (t: string) => { const low = t.toLowerCase(); for (const p of P.risk_patterns) if (low.includes(p.toLowerCase())) return p; return null; };
const rowText = (page: any, key: string) => page.evaluate((k: string) => { const el = Array.from(document.querySelectorAll('*')).find(e => e.children.length === 0 && ((e as HTMLElement).innerText || '').trim() === k); let b: Element | null = el || null; for (let i = 0; i < 3 && b?.parentElement; i++) b = b.parentElement; return b ? (b as HTMLElement).innerText.replace(/\s+/g, ' ').slice(0, 160) : null; }, key);
let client: any = null; let page: any = null;
async function main() {
  client = await connect();
  page = await client.page(P.page_name, { viewport: { width: 1440, height: 900 } });
  mark("goto");
  await page.goto(P.upload_url, { waitUntil: "domcontentloaded", timeout: 45000 });
  await waitForPageLoad(page); await page.waitForTimeout(2500);
  if (!/creator\.douyin\.com/.test(page.url()) || /login|passport/.test(page.url())) { out.error = "not logged in: " + page.url().slice(0, 120); out.login_expired = true; return; }
  let t = await bodyText(page); let r = riskIn(t); if (r) { out.risk = r; out.error = "risk signal on upload page: " + r; return; }
  if (/是否继续编辑/.test(t)) { mark("discard_previous_draft"); await page.getByText('放弃', { exact: true }).first().click({ timeout: 8000 }).catch(() => {}); await page.waitForTimeout(1200); }
  mark("upload");
  const t0 = Date.now();
  await page.locator('input[type=file]').first().setInputFiles(P.file_path);
  await page.locator('input[placeholder*="作品标题"]').waitFor({ state: 'visible', timeout: 120000 });
  mark("form", { ms: Date.now() - t0 });
  mark("title");
  await page.locator('input[placeholder*="作品标题"]').fill(P.title);
  mark("intro");
  const desc = page.locator('.zone-container[contenteditable=true], [contenteditable=true].editor-kit-container').first();
  await desc.click(); await page.keyboard.press('Control+A'); await page.keyboard.press('Delete');
  if (P.intro) await page.keyboard.type(P.intro, { delay: 8 });
  for (const tag of P.topics) { await page.keyboard.type(" #" + tag + " ", { delay: 25 }); await page.waitForTimeout(350); await page.keyboard.press('Escape').catch(() => {}); }
  if (P.declaration_label) {
    mark("declaration");
    await page.getByText('请选择自主声明', { exact: true }).first().click({ timeout: 10000 });
    await page.waitForTimeout(900);
    const modal = page.locator('.semi-modal-wrap, [role=dialog]').last();
    await modal.locator('label.semi-radio').filter({ hasText: P.declaration_label }).first().click({ timeout: 10000 });
    await page.waitForTimeout(300);
    await modal.getByRole('button', { name: '确定' }).click({ timeout: 10000 });
    await page.waitForTimeout(800);
    out.declaration_row = ((await bodyText(page)).match(/自主声明\s*([^\n]{0,24})/) || [])[1] || null;
  }
  if (P.hot_word) {
    mark("hot_word");
    try {
      await page.getByText('点击输入热点词', { exact: true }).first().click({ timeout: 8000 });
      await page.waitForTimeout(600);
      await page.keyboard.type(P.hot_word, { delay: 50 }); await page.waitForTimeout(2500);
      const opt = page.locator('[role=option], [class*=option], [class*=suggest] li, [class*=dropdown] li').filter({ hasText: P.hot_word }).first();
      if (await opt.count()) { await opt.click({ timeout: 5000 }); out.hot_pick = 'option'; } else { await page.keyboard.press('Enter'); out.hot_pick = 'enter'; }
      await page.waitForTimeout(700);
      out.hot_row = ((await bodyText(page)).match(/关联热点\s*([^\n]{0,40})/) || [])[1] || null;
      out.hot_word_attached = !!(out.hot_row && out.hot_row.includes(P.hot_word));
      if (!out.hot_word_attached) await page.keyboard.press('Escape').catch(() => {});
    } catch (e) { out.hot_word_error = String(e).slice(0, 160); await page.keyboard.press('Escape').catch(() => {}); }
  }
  const pickRadio = async (label: string) => page.evaluate((lab: string) => {
    const inputs = Array.from(document.querySelectorAll('input[type=checkbox], input[type=radio], input.radio-native-p6VBGt')) as HTMLInputElement[];
    const hit = inputs.find(i => (((i.closest('label') || i.parentElement) as HTMLElement | null)?.innerText || '').trim().startsWith(lab));
    if (!hit) return { found: false };
    hit.click();
    return { found: true, checked: hit.checked };
  }, label);
  mark("visibility");
  out.visibility = await pickRadio(P.visibility_label);
  await page.waitForTimeout(400);
  if (!out.visibility.checked) { await page.getByText(P.visibility_label, { exact: true }).first().click({ force: true }).catch(() => {}); await page.waitForTimeout(400); out.visibility = await page.evaluate((lab: string) => { const inputs = Array.from(document.querySelectorAll('input.radio-native-p6VBGt')) as HTMLInputElement[]; const hit = inputs.find(i => (((i.closest('label') || i.parentElement) as HTMLElement | null)?.innerText || '').trim().startsWith(lab)); return { found: !!hit, checked: !!hit?.checked }; }, P.visibility_label); }
  if (P.schedule_at) {
    mark("schedule");
    out.schedule_radio = await pickRadio('定时发布');
    await page.waitForTimeout(900);
    const dateInput = page.locator('input[placeholder="日期和时间"], input[placeholder*="时间"]').first();
    if (await dateInput.count()) { await dateInput.click(); await page.keyboard.press('Control+A'); await page.keyboard.type(P.schedule_at, { delay: 30 }); await page.keyboard.press('Enter'); await page.waitForTimeout(600); out.schedule_value = await dateInput.inputValue().catch(() => null); }
    else out.schedule_error = "date input not found";
    out.schedule_row = ((await bodyText(page)).match(/发布时间\s*([^\n]{0,60})/) || [])[1] || null;
  }
  mark("wait_upload");
  const tu = Date.now(); let up: any = null; let quiet = 0; let lastPct: string | null = null;
  while (Date.now() - tu < P.upload_timeout_ms) {
    up = await page.evaluate(() => { const t = document.body.innerText; const v = document.querySelector('video') as HTMLVideoElement | null; return { pct: (t.match(/(?:上传中|上传)[^\n]{0,12}?(\d{1,3}%)/) || t.match(/\b(\d{1,3}%)\b/) || [])[1], uploading: /上传中/.test(t), failed: /上传失败/.test(t), reupload: /重新上传/.test(t), video: v ? { src: !!(v.currentSrc || v.src), duration: v.duration || 0, ready: v.readyState } : null }; });
    if (up.failed) { out.error = "upload failed"; return; }
    if (up.pct) { lastPct = up.pct; quiet = 0; } else quiet++;
    // done = no progress text for a while, "重新上传" offered, and the preview player has media
    const previewReady = !!(up.video && (up.video.duration > 0 || up.video.ready > 0 || up.video.src));
    if (quiet >= 3 && up.reupload && !up.uploading && previewReady) break;
    if (quiet >= 45 && up.reupload && !up.uploading) break;  // no player in this layout: trust the quiet text
    await page.waitForTimeout(1000);
  }
  out.upload = { ...up, last_pct: lastPct, ms: Date.now() - tu };
  if (!(up && up.reupload && !up.uploading && !up.pct)) { out.error = "upload did not finish within " + P.upload_timeout_ms + "ms (last " + lastPct + ")"; return; }
  out.summary = await page.evaluate(() => ({ title: (document.querySelector('input[placeholder*="作品标题"]') as HTMLInputElement)?.value, intro: (document.querySelector('.zone-container') as HTMLElement)?.innerText?.replace(/​/g, '').replace(/\s+/g, ' ').slice(0, 300), counters: (document.body.innerText.match(/\d+\/30|\d+ \/ 1000/g) || []), check: (document.body.innerText.match(/作品未见异常|检测中|存在风险[^\n]{0,30}|违规[^\n]{0,30}/) || [])[0] }));
  t = await bodyText(page); r = riskIn(t.replace(/无需添加自主声明|作品未见异常/g, ''));
  if (r) { out.risk = r; out.error = "risk signal before publish: " + r; }
  if (P.simulate_risk) { await page.evaluate(() => { const d = document.createElement('div'); d.id = 'obx-fake-captcha'; d.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:99999;display:flex;align-items:center;justify-content:center;color:#fff;font-size:28px'; d.innerText = '安全验证：请完成滑动验证码'; document.body.appendChild(d); }); await page.waitForTimeout(300); t = await bodyText(page); r = riskIn(t); out.risk = r; out.error = "risk signal (simulated): " + r; }
  mark("evidence");
  try { await page.screenshot({ path: P.evidence_path, fullPage: true }); out.evidence = P.evidence_path; } catch (e) { out.evidence_error = String(e).slice(0, 120); }
  if (out.risk) { return; }
  if (P.dry_run) {
    mark("save_draft");
    await page.getByRole('button', { name: '暂存离开' }).first().click({ timeout: 10000 });
    await page.waitForTimeout(3500);
    out.final_url = page.url(); out.ok = true; return;
  }
  mark("publish");
  const before = page.url();
  await page.getByRole('button', { name: '发布', exact: true }).first().click({ timeout: 10000 });
  const tp = Date.now();
  while (Date.now() - tp < 45000) {
    await page.waitForTimeout(1000);
    const txt = await bodyText(page);
    const rr = riskIn(txt.replace(/无需添加自主声明|作品未见异常/g, ''));
    if (rr) { out.risk = rr; out.error = "risk signal after publish click: " + rr; out.final_url = page.url(); try { await page.screenshot({ path: P.evidence_path.replace('.png', '-risk.png'), fullPage: true }); out.risk_evidence = P.evidence_path.replace('.png', '-risk.png'); } catch {} return; }
    if (page.url() !== before && /content\/manage/.test(page.url())) break;
    if (/发布成功|已发布/.test(txt)) break;
  }
  out.final_url = page.url(); out.publish_ms = Date.now() - tp;
  mark("readback");
  if (!/content\/manage/.test(page.url())) { await page.goto(P.manage_url, { waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => {}); await waitForPageLoad(page).catch(() => {}); }
  await page.waitForTimeout(4000);
  out.manage_text = ((await bodyText(page)) || '').replace(/\s+/g, ' ').slice(0, 400);
  out.listed = out.manage_text.includes(P.title.slice(0, 12));
  // The manage page fetches /janus/douyin/creator/pc/work_list itself (signed URL);
  // replaying that exact URL in page context returns the newest works with aweme_id.
  for (let attempt = 0; attempt < 3 && !out.item_id; attempt++) {
    const rb = await page.evaluate(async (title: string) => {
      const urls = performance.getEntriesByType('resource').map(e => e.name).filter(u => /work_list/.test(u));
      const u = urls[urls.length - 1]; if (!u) return { error: 'no work_list request seen' };
      try {
        const r = await fetch(u, { credentials: 'include' }); const j = await r.json();
        const list = (j.aweme_list || []) as any[];
        const hit = list.find(o => (o.item_title || '') === title || (o.desc || '').startsWith(title)) || null;
        return { status: r.status, n: list.length, hit: hit ? { aweme_id: String(hit.aweme_id || ''), share_url: hit.share_url || null, status_value: hit.status_value, create_time: hit.create_time } : null, newest_title: list[0]?.item_title || null };
      } catch (e) { return { error: String(e).slice(0, 160) }; }
    }, P.title);
    out.readback = rb;
    if (rb && rb.hit && rb.hit.aweme_id) { out.item_id = rb.hit.aweme_id; out.item_url = "https://www.douyin.com/video/" + rb.hit.aweme_id; out.share_url = rb.hit.share_url; out.item_status_value = rb.hit.status_value; break; }
    await page.waitForTimeout(3000);
    await page.reload({ waitUntil: "domcontentloaded" }).catch(() => {}); await page.waitForTimeout(4000);
  }
  out.ok = !!out.item_id || out.listed || /发布成功|已发布|审核/.test(out.manage_text);
  if (!out.ok) out.error = "publish outcome unclear: url=" + page.url().slice(0, 100);
}
main().catch(e => { out.error = (out.error ? out.error + " | " : "") + String(e).replace(/\n[\s\S]*/, "").slice(0, 300); })
  .finally(async () => { try { if (client) await client.disconnect(); } catch {} console.log("OBX_RESULT " + JSON.stringify(out)); });
'''


def build_params(*, file_path: str, title: str, intro: str, topics: list[str], declaration: str, hot_word: str | None,
                 visibility: str, schedule_at: str | None, dry_run: bool, simulate_risk: bool, risk_patterns: list[str],
                 upload_timeout_seconds: int, evidence_path: str, page_name: str = "obx-publish") -> dict:
    return {
        "upload_url": UPLOAD_URL, "manage_url": MANAGE_URL, "page_name": page_name, "file_path": file_path,
        "title": title, "intro": intro, "topics": topics,
        "declaration_label": DECLARATIONS[declaration] if declaration != "none" else DECLARATIONS["none"],
        "hot_word": hot_word, "visibility_label": VISIBILITY[visibility], "schedule_at": schedule_at,
        "dry_run": dry_run, "simulate_risk": simulate_risk, "risk_patterns": risk_patterns,
        "upload_timeout_ms": upload_timeout_seconds * 1000, "evidence_path": evidence_path,
    }


def build_script(params: dict) -> str:
    return SCRIPT.replace("__PARAMS__", json.dumps(params, ensure_ascii=False))


def build_command(params: dict, *, skill_dir: str = "/opt/openbox/skills/dev-browser", timeout_s: int = 600) -> str:
    """One shell command: write the script to a temp file and run it with tsx from the skill dir."""
    import base64
    import gzip

    encoded = base64.b64encode(gzip.compress(build_script(params).encode())).decode()
    return (
        f": obx-desktop-publish; cd {skill_dir} && mkdir -p tmp && printf %s {encoded} | base64 -d | gunzip > tmp/obx-publish.ts && "
        f"PATH=/usr/local/bin:$PATH timeout {timeout_s} npx tsx tmp/obx-publish.ts 2>&1"
    )


def parse_output(stdout: str) -> dict:
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("OBX_RESULT "):
            return json.loads(line[len("OBX_RESULT "):])
    raise ValueError("publish script printed no result: " + (stdout or "")[-300:])
