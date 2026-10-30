#!/usr/bin/env python3
"""Drive the dashboard in headless Chromium and record the screen (Playwright).

Expects a running app (see make_demo.sh). Each scene lasts at least as long as its
narration (durations.json from tts.py). Long waits for the agent are logged as cuts
so assemble.py can remove dead air. Writes <out>/raw.webm and <out>/timeline.json.

PayPal Sandbox buyer approval: if SANDBOX_BUYER_EMAIL and SANDBOX_BUYER_PASSWORD are set
(a Sandbox *personal* test account from developer.paypal.com; pass them only as env vars, they
are never printed or written), the script logs in on the real Sandbox checkout, clicks Pay and
follows PayPal's redirect back to /paypal/return. The screen region with the buyer's email is
logged as a blur window in timeline.json (assemble.py blurs it). Without them, the draft shows
the real checkout page and then a clearly labelled placeholder card for that manual step.
"""
import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

W, H = 1280, 720
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never proxy localhost
DEMO_REQUEST = ("Buy 2 USB-C charging cables and a mechanical keyboard for our new hire, "
                "and also a $100 gift card as a welcome present.")

CARD_CSS = """
body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
 background:linear-gradient(135deg,#0b1f3a,#123a6b);font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#fff}
.c{max-width:1040px;padding:40px}
h1{font-size:64px;margin:0 0 12px} h2{font-size:40px;margin:0 0 24px}
p{font-size:28px;line-height:1.4;color:#d6e4ff;margin:8px 0}
table{border-collapse:collapse;font-size:26px;margin-top:12px} td{padding:10px 18px;border-bottom:1px solid #2c5288}
.ok{color:#7ee2a8;font-weight:700}.wait{color:#ffd27a;font-weight:700}.no{color:#ff8f8f;font-weight:700}
.tag{display:inline-block;background:#ffd27a;color:#3a2a00;padding:6px 14px;border-radius:8px;font-weight:700;font-size:22px;margin-bottom:18px}
small{font-size:20px;color:#9fb6dc}
"""

CARDS = {
    "s1_title": """<div class="c"><h1>GuardRail Buyer</h1>
<p>An AI purchasing agent that pays with <b>PayPal</b>, but only inside the spend guardrails you set.</p>
<p><small>PayPal AI Hackathon 2026 · PayPal Sandbox · Claude · AG Grid</small></p></div>""",
    "s2_policy": """<div class="c"><h2>A deterministic policy gate between the LLM and the wallet</h2>
<table>
<tr><td>Within per-order cap ($50) and daily budget ($150)</td><td class="ok">ALLOW → PayPal order</td></tr>
<tr><td>Over a cap</td><td class="wait">NEEDS APPROVAL → a human decides</td></tr>
<tr><td>Blocked merchant / category, over hard max ($500)</td><td class="no">DENY → never escalated</td></tr>
</table><p><small>Enforced server-side on every order. The model only gets tools, never PayPal credentials.</small></p></div>""",
    "s5_return": """<div class="c"><span class="tag">DRAFT PLACEHOLDER · manual step</span>
<h2>Buyer approves on the PayPal Sandbox</h2>
<p>To be recorded by hand: log in with the Sandbox <b>personal test account</b> → Pay →
PayPal redirects to <code>/paypal/return</code> → the app captures → order <b>COMPLETED</b> with its capture id.</p>
<p><small>Not automated here: the Sandbox buyer password is not available to the recording script.</small></p></div>""",
    "s8_close": """<div class="c"><h1>GuardRail Buyer</h1>
<p>Claude tool use · PayPal Orders v2 (Sandbox) · FastAPI · AG Grid Community</p>
<p>Open source (MIT) · 400+ automated tests · <b>AGENT_MODE=offline</b> runs the full flow without an AI key</p>
<p style="margin-top:28px;font-size:34px;color:#fff"><b>Let agents spend, inside your guardrails.</b></p>
<p><small>Narration: Piper TTS (voice trained on LibriTTS-R, CC BY 4.0). No music.</small></p></div>""",
}

CAPTION_JS = """(t) => { let d = document.getElementById('__cap');
 if (!d) { d = document.createElement('div'); d.id='__cap';
   d.style.cssText='position:fixed;left:24px;bottom:24px;z-index:2147483647;'+
   'background:rgba(10,25,50,.88);color:#fff;font:600 22px/1.35 -apple-system,Segoe UI,Roboto,Arial,sans-serif;'+
   'padding:10px 20px;border-radius:10px;max-width:720px;text-align:left;box-shadow:0 4px 18px rgba(0,0,0,.3)';
   document.body.appendChild(d); }
 d.textContent = t; d.style.display = t ? 'block' : 'none'; }"""


def proxy_settings():
    url = urllib.request.getproxies().get("https")
    if not url:
        return None
    pu = urllib.parse.urlparse(url)
    p = {"server": f"{pu.scheme}://{pu.hostname}:{pu.port}", "bypass": "<-loopback>,localhost,127.0.0.1"}  # Chromium proxies loopback unless <-loopback>
    if pu.username:
        p.update(username=urllib.parse.unquote(pu.username), password=urllib.parse.unquote(pu.password or ""))
    return p


class Recorder:
    def __init__(self, page, durations, t0):
        self.page, self.durs, self.t0 = page, durations, t0
        self.timeline, self.cuts, self.blurs = [], [], []

    def now(self):
        return time.monotonic() - self.t0

    def cap(self, text):
        try:
            self.page.evaluate(CAPTION_JS, text)
        except Exception:
            pass

    def start(self, sid):
        self.cur = {"id": sid, "start": round(self.now(), 2)}

    def end(self, pad=0.7):
        need = self.cur["start"] + self.durs.get(self.cur["id"], 3) + pad
        while self.now() < need:
            time.sleep(0.05)
        self.cur["end"] = round(self.now(), 2)
        self.timeline.append(self.cur)

    def card(self, sid):
        self.start(sid)
        self.page.set_content(f"<html><head><style>{CARD_CSS}</style></head><body>{CARDS[sid]}</body></html>")
        self.end()


def first_visible(page, sels, timeout_ms):
    end = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < end:
        for s in sels:
            loc = page.locator(s)
            try:
                if loc.count() and loc.first.is_visible():
                    return loc.first
            except Exception:
                pass
        page.wait_for_timeout(250)
    return None


def sandbox_buyer_pay(page, r, base, email, password):
    """Log in on the real Sandbox checkout, click Pay, follow the redirect back to the app."""
    r.cap("Buyer logs in with the Sandbox personal test account")
    b0 = r.now()
    em = first_visible(page, ["#email", "input[name=login_email]", "input[type=email]"], 30_000)
    if em is None:
        raise RuntimeError("Sandbox login: no email field")
    eb = em.bounding_box()
    em.click(); em.fill(""); em.type(email, delay=25)
    nxt = first_visible(page, ["#btnNext", "button:has-text('Next')"], 3000)
    if nxt:
        nxt.click()
    pw = first_visible(page, ["#password", "input[name=login_password]", "input[type=password]"], 30_000)
    if pw is None:
        raise RuntimeError("Sandbox login: no password field")
    pb = pw.bounding_box()
    # region holding the email field (login step 1) and the email line above the password field (step 2)
    top = int(min(eb["y"], pb["y"] - 60) - 12)
    bottom = int(max(eb["y"] + eb["height"], pb["y"] - 2))
    email_box = [420, max(0, top), 440, bottom - max(0, top)]
    pw.click(); pw.type(password, delay=40)
    page.wait_for_timeout(500)
    first_visible(page, ["#btnLogin", "button:has-text('Log In')", "button[type=submit]"], 5000).click()
    t_login = r.now()
    btn, end = None, time.monotonic() + 60
    while btn is None and time.monotonic() < end:  # the review page's main button is named exactly "Pay"
        for loc in (page.get_by_role("button", name="Pay", exact=True),
                    page.get_by_role("button", name="Complete Purchase", exact=True)):
            try:
                if loc.count() and loc.first.is_visible():
                    btn = loc.first
                    break
            except Exception:
                pass
        else:
            page.wait_for_timeout(250)
    if btn is None:
        raise RuntimeError("Sandbox review page: no Pay button")
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    t_review = r.now()
    r.blurs.append([round(max(0.0, b0 - 0.5), 2), round(t_review + 0.6, 2), *email_box])  # padded: raw video starts slightly before t0
    if t_review - t_login > 2.5:  # dead air while PayPal logs in
        r.cuts.append([round(t_login + 1.0, 2), round(t_review - 0.8, 2)])
    r.cap("Review the $25.98 Sandbox payment → Pay")
    page.wait_for_timeout(2500)
    btn.hover(); page.wait_for_timeout(600)
    btn.click()
    t_pay = r.now()
    page.wait_for_url(base + "/**", timeout=90_000)
    page.wait_for_selector("#kpis .kpi", timeout=30_000)
    if r.now() - t_pay > 3.0:
        r.cuts.append([round(t_pay + 1.2, 2), round(r.now() - 0.6, 2)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8011")
    ap.add_argument("--token", default="demo")
    ap.add_argument("--durations", default="build/video/durations.json")
    ap.add_argument("--out", default="build/video")
    ap.add_argument("--request", default=DEMO_REQUEST)
    ap.add_argument("--chromium", default="/usr/bin/chromium")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    durs = json.loads(Path(a.durations).read_text())["durations"]
    notes = []

    with sync_playwright() as p:
        kw = {"args": ["--no-sandbox"]}
        if a.chromium and Path(a.chromium).exists():
            kw["executable_path"] = a.chromium
        if proxy_settings():
            kw["proxy"] = proxy_settings()
        browser = p.chromium.launch(**kw)
        ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(out / "rawvid"),
                                  record_video_size={"width": W, "height": H})
        page = ctx.new_page()
        t0 = time.monotonic()
        r = Recorder(page, durs, t0)

        r.card("s1_title")
        r.card("s2_policy")

        # s3 dashboard
        r.start("s3_dashboard")
        page.goto(a.base + "/")
        page.fill("#token", a.token)
        page.dispatch_event("#token", "change")
        page.wait_for_selector("#kpis .kpi")
        r.cap("Live dashboard · PayPal mode: Sandbox (api-m.sandbox.paypal.com)")
        page.hover("#kpis")
        r.end()

        # s4 ask the agent
        r.start("s4_ask")
        r.cap("")
        page.click("#ask")
        page.type("#ask", a.request, delay=22)
        page.click("#ask-btn")
        r.cap("Claude is calling tools: search_catalog → check_policy → create_order / request_approval")
        page.wait_for_selector("#answer .steps", timeout=180_000)
        t_ans = r.now()
        narr_end = r.cur["start"] + durs["s4_ask"] + 0.5
        if t_ans - 0.8 > narr_end + 0.5:
            r.cuts.append([round(narr_end, 2), round(t_ans - 0.8, 2)])
        page.wait_for_timeout(600)
        r.cur["end"] = round(r.now(), 2)
        r.timeline.append(r.cur)

        r.start("s4_result")
        r.cap("ALLOW → real Sandbox order · over cap → approval request · blocked → refused")
        page.locator("#answer").scroll_into_view_if_needed()
        page.wait_for_timeout(4500)
        page.mouse.wheel(0, 420)
        page.wait_for_timeout(4000)
        page.mouse.wheel(0, 380)
        r.end()

        # s5 real Sandbox checkout for the ALLOWed order
        orders = json.loads(LOCAL.open(a.base + "/orders").read())
        cable = next((o for o in orders if o["product_id"] == "P-1001" and o.get("approve_url")), None)
        buyer = (os.environ.get("SANDBOX_BUYER_EMAIL"), os.environ.get("SANDBOX_BUYER_PASSWORD"))
        paid = False
        r.start("s5_checkout")
        if cable:
            page.goto(cable["approve_url"], timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            r.cap(f"Real PayPal Sandbox checkout for {cable['id']} (${cable['amount']}) · sandbox, no real money")
            if all(buyer):
                page.wait_for_timeout(1500)
                try:
                    sandbox_buyer_pay(page, r, a.base, *buyer)
                    paid = True
                except Exception as e:
                    notes.append(f"sandbox buyer login/pay failed: {type(e).__name__}: {str(e).splitlines()[0][:120]}")
        else:
            notes.append("no PAYER_ACTION_REQUIRED cable order found (agent may have chosen differently)")
        r.end()
        if paid:
            r.start("s5_return")
            r.cap("PayPal redirects to /paypal/return → the app captures → COMPLETED")
            page.wait_for_timeout(3500)
            page.locator("#orders").scroll_into_view_if_needed()
            page.wait_for_timeout(1200)
            r.cap("Order COMPLETED with its PayPal capture id · reserved → captured spend")
            r.end(pad=1.5)
        else:
            r.card("s5_return")

        # s6 approve the keyboard
        r.start("s6_approve")
        page.goto(a.base + "/")
        page.wait_for_selector("#kpis .kpi")
        page.locator("#approvals").scroll_into_view_if_needed()
        r.cap("Human in the loop: approver token required")
        page.wait_for_timeout(3500)
        btn = page.locator("#approvals button.ok").first
        if btn.count():
            btn.hover()
            page.wait_for_timeout(800)
            try:
                with ctx.expect_page(timeout=8000) as pop:
                    btn.click()
                pop.value.close()  # the dashboard opens the PayPal checkout in a new tab
            except Exception:
                pass
            page.bring_to_front()
            r.cap("Approved → deny rules re-checked → new PayPal order awaiting the buyer")
            page.wait_for_timeout(1200)
            page.locator("#orders").scroll_into_view_if_needed()
        else:
            notes.append("no pending approval to click")
        r.end()

        # s7 buyer cancels on PayPal -> reservation released
        orders = json.loads(LOCAL.open(a.base + "/orders").read())
        kb = next((o for o in orders if o["product_id"] == "P-1003" and o["status"] == "PAYER_ACTION_REQUIRED"), None)
        r.start("s7_cancel")
        if kb:
            page.goto(kb["approve_url"], timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            r.cap("Buyer clicks “Cancel and return” on the real Sandbox checkout")
            page.wait_for_timeout(3000)
            r.cap("")
            try:
                link = page.locator("#cancelLink")
                if link.count():
                    link.scroll_into_view_if_needed()
                    page.wait_for_timeout(500)
                    link.evaluate("e => e.click()")  # the sticky footer overlaps it in a 720p viewport
                else:  # a buyer already logged in sees "Return to Test Store" instead
                    page.get_by_text(re.compile(r"Cancel and return|Return to ")).first.click(timeout=8000)
                page.wait_for_url(a.base + "/**", timeout=30_000)
                page.wait_for_selector("#kpis .kpi")
                r.cap("Back in the app: order CANCELLED, reserved budget released")
                page.locator("#orders").scroll_into_view_if_needed()
                page.wait_for_timeout(3500)  # hold on the result even when the narration is already done
            except Exception as e:
                notes.append(f"cancel link failed: {e!r}"[:200])
        else:
            notes.append("no keyboard order awaiting payer")
        r.end()

        r.card("s8_close")
        total = r.now()
        page.close()
        video = page.video.path()
        ctx.close()
        browser.close()

    raw = out / "raw.webm"
    Path(video).replace(raw)
    (out / "timeline.json").write_text(json.dumps(
        {"video": str(raw), "total": round(total, 2), "scenes": r.timeline, "cuts": r.cuts,
         "blurs": r.blurs, "notes": notes}, indent=1))
    print(json.dumps({"total": round(total, 1), "cuts": r.cuts, "notes": notes}))


if __name__ == "__main__":
    main()
