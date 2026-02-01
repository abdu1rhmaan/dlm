import asyncio
import time
import json
import sqlite3
from pathlib import Path
from playwright.async_api import async_playwright
import traceback
from urllib.parse import urlparse

# Import necessary tools from the application
from dlm.bootstrap import create_container
from dlm.app.commands import AddDownload

class ReputationManager:
    """نظام سمعة ذكي لإدارة النطاقات بشكل تكيفي بناءً على السلوك والسمعة."""
    def __init__(self):
        self.stats = {}  # host: {reqs, cross, sus, downloads}
        self.trusted = set()

    def get_score(self, url, referer, resource_type):
        try:
            parsed = urlparse(url)
            host = parsed.netloc
            if not host: return 0
            if host in self.trusted: return 0

            s = self.stats.setdefault(host, {'reqs': 0, 'cross': 0, 'sus': 0, 'downloads': 0})
            s['reqs'] += 1

            score = 0
            
            # 1. التقييم العابر للمجالات (Cross-domain scoring)
            if referer:
                ref_host = urlparse(referer).netloc
                if ref_host and ref_host != host:
                    score += 2
                    s['cross'] += 1
            
            # 2. معاملات التتبع (Tracking parameters)
            tracking_params = ['utm_', 'gclid', 'fbclid', 'clickid', 'ref', 'track', 'cid', 'sid']
            if any(p in url for p in tracking_params):
                score += 1
                s['sus'] += 1

            # 3. نوع المورد (Resource type) - السكربتات العابرة للمواقع مشبوهة أكثر
            if resource_type in ['script', 'sub_frame'] and score >= 2:
                score += 1

            # 4. تاريخ السمعة (Reputation history) - المكررين للحجب
            if s['reqs'] > 10 and (s['cross'] / s['reqs']) > 0.6:
                score += 2
            
            # تسجيل النتائج للمتابعة
            # if score >= 6:
            #     print(f"[AdAI] host={host} score={score} (BLOCKED)")
            # elif score >= 3:
            #     print(f"[AdAI] host={host} score={score} (SOFT BLOCK)")
            
            return score
        except:
            return 0

    def add_download_success(self, host):
        """منح الثقة للنطاقات التي تنجح في تنزيل ملفات حقيقية."""
        if not host: return
        s = self.stats.setdefault(host, {'reqs': 0, 'cross': 0, 'sus': 0, 'downloads': 0})
        s['downloads'] += 1
        if s['downloads'] >= 3 and host not in self.trusted:
            self.trusted.add(host)
            # print(f"[AdAI] Domain TRUSTED (Adaptive): {host}")

def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent

def get_db() -> Path:
    """Get the database path."""
    return get_project_root() / "dlm.db"

async def browser_command(target_url: str = None):
    """المحرك الرئيسي لتشغيل المتصفح واصطياد الروابط والتحميلات."""
    project_root = get_project_root()
    profiles_dir = project_root / "data" / "browser_profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    
    current_profile = profiles_dir / f"dlmb_{int(time.time())}"
    
    # تحضير نظام السمعة والوعاء (Container)
    container = create_container()
    rep_manager = ReputationManager()
    
    async with async_playwright() as p:
        print("[Browser] Launching Chromium (Adaptive Shield v4.0 Active)...")
        
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(current_profile),
            headless=False,
            viewport={"width": 1280 + int(time.time() % 100), "height": 800 + int(time.time() % 50)},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", 
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-features=TranslateUI,SitePerProcess",
                "--disable-web-security",
                "--disable-site-isolation-trials",
                "--lang=en-US,en;q=0.9",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-client-side-phishing-detection",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-hang-monitor",
                "--disable-popup-blocking",
                "--disable-prompt-on-repost",
                "--disable-sync"
            ],
            ignore_default_args=["--enable-automation", "--enable-blink-features"],
            bypass_csp=True,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Upgrade-Insecure-Requests": "1"
            }
        )

        # Tweak: Track last request headers for each URL to capture full download context
        last_requests = {} # url: {method, headers, cookies}

        async def on_request(request):
            try:
                url = request.url
                # We only care about potential download URLs or high-level navigation
                if request.resource_type in ["document", "other", "media"]:
                    last_requests[url] = {
                        "method": request.method,
                        "headers": await request.headers_array(),
                    }
            except: pass

        context.on("request", on_request)

        async def handle_download(download):
            """التعامل مع حدث التحميل واختطافه لصالح DLM."""
            try:
                await download.cancel()
                
                url = download.url
                suggested_filename = download.suggested_filename
                size = 0 
                page = download.page
                referrer = page.url
                storage_state = await context.storage_state()
                user_agent = await page.evaluate("navigator.userAgent")
                
                # Fetch captured request metadata if available
                req_meta = last_requests.get(url, {"method": "GET", "headers": {}})
                
                # Extract cookies in a more accessible format for curl_cffi
                cookies = storage_state.get("cookies", [])
                
                # تخزين الرابط في قاعدة البيانات
                capture_id = container["service"].repository.add_browser_download(
                    url, suggested_filename, size, referrer, json.dumps(storage_state), user_agent,
                    method=req_meta["method"],
                    headers_json=json.dumps(req_meta["headers"]),
                    cookies_json=json.dumps(cookies),
                    source_url=referrer
                )
                
                # تحديث نظام الثقة التكيفي
                try:
                    host = urlparse(referrer).netloc
                    rep_manager.add_download_success(host)
                except: pass

                # بدء تحليل الحجم في الخلفية باستخدام بروب حقيقي (1DM-style)
                def perform_browser_probe(cid, u, ref, h, c, ua):
                    try:
                        # print(f"[Browser Probe] ▶ Starting real download probe for: {u}")
                        service = container["service"]
                        
                        # 1. Check if already probed
                        dl = service.get_download_by_capture_id(cid)
                        if dl and dl.browser_probe_done:
                            # print(f"[Browser Probe] ℹ Probe already completed for this task. Skipping.")
                            return

                        # 2. Get session data for curl_cffi
                        final_headers, final_cookies = service.network._add_browser_headers(u, ref, h, c, ua)
                        
                        import requests as clean_requests
                        try:
                            from curl_cffi import requests as curl_requests
                            HAVE_CURL_CFFI = True
                        except (ImportError, Exception):
                            curl_requests = clean_requests
                            HAVE_CURL_CFFI = False
                        
                        
                        # print(f"[Browser Probe] ▶ Using Range: bytes=0-0")
                        session_args = {"impersonate": "chrome120"} if HAVE_CURL_CFFI else {}
                        s = curl_requests.Session(**session_args)
                        try:
                            # Add Range header
                            h_range = dict(final_headers)
                            h_range["Range"] = "bytes=0-0"
                            
                            r = s.get(u, headers=h_range, cookies=final_cookies, stream=True, timeout=(10, 30), verify=False)
                            # print(f"[Browser Probe] ▶ Response status: {r.status_code}")
                            
                            # Extract size from Content-Range or Content-Length
                            length = r.headers.get("Content-Length")
                            found_size = None
                            
                            if "Content-Range" in r.headers:
                                cr = r.headers["Content-Range"]
                                # print(f"[Browser Probe] ▶ Content-Range: {cr}")
                                if '/' in cr:
                                    total = cr.split('/')[-1]
                                    if total.isdigit():
                                        found_size = int(total)
                            
                            if not found_size and length and str(length).isdigit():
                                found_size = int(length)

                            if found_size:
                                print(f"[Browser Probe] ✅ File size resolved: {found_size} bytes")
                                # Update DB
                                service.repository.update_browser_download_size(cid, found_size)
                                # Sync task
                                all_dls = service.repository.get_all()
                                target_dl = next((d for d in all_dls if d.browser_capture_id == cid), None)
                                if target_dl:
                                    target_dl.total_size = found_size
                                    target_dl.browser_probe_done = True
                                    target_dl.resumable = True # If range worked, it's resumable
                                    service._initialize_segments(target_dl)
                                    service.repository.save(target_dl)
                            else:
                                # print(f"[Browser Probe] ⚠ Could not resolve size via probe.")
                                pass
                            
                            r.close()
                            # print(f"[Browser Probe] ⛔ Probe connection closed immediately")
                        finally:
                            s.close()
                    except Exception as ex:
                        print(f"[Browser Probe] ❌ Probe failed: {ex}")

                container["service"].executor.submit(perform_browser_probe, capture_id, url, referrer, req_meta["headers"], cookies, user_agent)
                
                print(f"\n[Browser Capture] ✅ Captured: {suggested_filename}")
                
                # إشعار داخل المتصفح
                await page.evaluate("""(name) => {
                    const notif = document.createElement('div');
                    notif.id = 'dlm-capture-notif';
                    notif.style = 'position:fixed;top:20px;right:20px;background:#4CAF50;color:white;padding:12px 20px;border-radius:6px;z-index:999999;font-family:system-ui;box-shadow: 0 4px 12px rgba(0,0,0,0.15);transition: opacity 0.5s;';
                    notif.innerHTML = `<b style="display:block;margin-bottom:4px;">✔ Download Captured</b> <span style="font-size:0.9em;opacity:0.9">${name}</span>`;
                    document.body.appendChild(notif);
                    setTimeout(() => {
                        notif.style.opacity = '0';
                        setTimeout(() => notif.remove(), 500);
                    }, 5000);
                }""", suggested_filename)
            except Exception as e:
                print(f"[Error] Failed to capture download: {e}")

        # --- Layer 1 - نظام الحماية السلوكي للشبكة (VCPL Enhanced) ---
        async def block_ads(route, request):
            url = request.url.lower()
            resource_type = request.resource_type
            referer = request.headers.get('referer', '')
            
            # استثناء المواقع الموثوقة (Adaptive Trust)
            try:
                host = urlparse(request.url).netloc
                if host in rep_manager.trusted:
                    await route.continue_()
                    return
            except: pass

            # استثناءات ذكية للتحميلات والمستندات
            dl_patterns = ['download', 'file', '.zip', '.rar', '.mp4', '.mkv', '.pdf', 'getlink', 'direct', 'torrent']
            if any(p in url for p in dl_patterns) or resource_type in ["document", "media"]:
                await route.continue_()
                return

            # حساب درجة الشك بناءً على السلوك
            score = rep_manager.get_score(url, referer, resource_type)
            
            # VCPL Network Rule: Do NOT block images/videos unless score is very high (>= 8)
            # This protects thumbnails/posters from low-score trackers/CDNs
            if resource_type in ["image", "video"] and score < 8:
                await route.continue_()
                return

            if score >= 6:
                await route.fulfill(status=204, body="")
            elif score >= 3:
                await route.fulfill(status=204, body="")
            else:
                await route.continue_()

        # تسجيل المعترض العالمي
        await context.route("**/*", block_ads)

        async def setup_page(page):
            """إعداد الصفحة بالدروع المتقدمة."""
            await page.route("**/*", block_ads)

            async def handle_popup(popup):
                url = popup.url
                dl_patterns = ['download', 'file', '.zip', '.rar', '.mp4', '.pdf', 'getlink', 'direct', 'torrent']
                try:
                    opener = await popup.opener()
                    is_same = urlparse(opener.url).netloc == urlparse(url).netloc if opener else False
                except: is_same = False

                if any(p in url.lower() for p in dl_patterns) or is_same:
                    pass # print(f"[Browser] [Popup Kept] {url}")
                else:
                    # print(f"[Browser] [Popup Blocked] {url}")
                    await popup.close()

            # page.on("console", lambda msg: print(f"[Browser JS] {msg.text}"))
            page.on("popup", handle_popup)
            
            # --- Shield v4.0 JS Injection (Click-Trap Detection & Performance Guard) ---
            shield_script = """
            (() => {
                console.log("Shield v4.0 (Behavioral AI) Activated");

                // --- حالة النظام ---
                window.__userGesture = false;
                window.__lastDOMScan = 0;
                window.__isInteracting = false;
                const dlPatterns = ['download', 'file', '.zip', '.rar', '.mp4', '.mkv', '.pdf', 'getlink', 'direct', 'torrent'];

                // تتبع تفاعل المستخدم الحقيقي
                window.addEventListener('mousedown', () => {
                    window.__userGesture = true;
                    setTimeout(() => window.__userGesture = false, 1200);
                }, true);
                
                window.addEventListener('scroll', () => {
                    window.__isInteracting = true;
                    setTimeout(() => window.__isInteracting = false, 500);
                }, {passive: true});

                // --- الطبقة 2: تنظيف الروابط من معاملات التتبع ---
                function sanitizeParams() {
                    const params = ['utm_source', 'utm_medium', 'utm_campaign', 'gclid', 'fbclid', 'clickid', 'ref', 'track', 'cid', 'sid'];
                    const url = new URL(window.location.href);
                    let changed = false;
                    params.forEach(p => { if (url.searchParams.has(p)) { url.searchParams.delete(p); changed = true; } });
                    if (changed) window.history.replaceState({}, '', url.toString());
                }

                // --- الطبقة 4: صائد مصائد النقرات (Click-Trap Detector) ---
                function neutralizeClickTraps() {
                    if (window.__isInteracting) return;
                    const vW = window.innerWidth;
                    const vH = window.innerHeight;
                    document.querySelectorAll('a, div, section, iframe').forEach(el => {
                        if (el.dataset.shielded || isSafeVisual(el)) return;
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        
                        // الفحص: طبقة كبيرة، شفافة، ثابتة، ولها Pointer ولكن بدون محتوى نصي
                        if (r.width >= vW * 0.8 && r.height >= vH * 0.8 && (s.position === 'fixed' || s.position === 'absolute')) {
                            const isInvisible = parseFloat(s.opacity) < 0.2 || s.visibility === 'hidden' || s.backgroundColor.includes('rgba(0, 0, 0, 0)');
                            const isPointer = s.cursor === 'pointer';
                            const hasText = el.innerText && el.innerText.trim().length > 10;

                            if (isPointer && !hasText && isInvisible) {
                                console.log("[Shield] Click-trap neutralized: " + el.tagName);
                                el.dataset.shielded = "true";
                                el.style.setProperty('pointer-events', 'none', 'important');
                            }
                        }
                    });
                }

                // --- VCPL: Visual Content Protection Layer ---
                function isSafeVisual(el) {
                    const tagName = el.tagName.toLowerCase();
                    const isMedia = ['img', 'video', 'picture', 'source', 'canvas'].includes(tagName);
                    const style = window.getComputedStyle(el);
                    const hasBg = style.backgroundImage !== 'none';
                    if (!isMedia && !hasBg) return false;

                    const r = el.getBoundingClientRect();
                    const isGoodSize = r.width > 120 && r.height > 120;
                    const isNotOverlay = style.position !== 'fixed' && style.position !== 'sticky' && parseFloat(style.zIndex) < 500;
                    
                    const safeContainers = 'article, main, section, figure, li, .card, .grid, .poster, .episode, .item, .result, .gallery';
                    const isInsideSafe = el.closest(safeContainers) !== null;

                    return (isInsideSafe && isGoodSize && isNotOverlay);
                }

                // --- الطبقة 1: Safe Neutralization ---
                function neutralize(el, label) {
                    if (el.dataset.shielded === "true") return;
                    if (isSafeVisual(el)) return; // VCPL Protection

                    console.log("[Shield] Neutralizing " + label + ": " + el.tagName);
                    el.dataset.shielded = "true";
                    el.style.setProperty('pointer-events', 'none', 'important');
                    el.style.setProperty('cursor', 'default', 'important');
                    el.style.setProperty('opacity', '0', 'important');
                    el.style.setProperty('visibility', 'hidden', 'important');
                }

                // --- تنظيف الـ DOM مع مراعاة الأداء ---
                function cleanDOM() {
                    if (window.__isInteracting || (Date.now() - window.__lastDOMScan < 1000)) return;
                    window.__lastDOMScan = Date.now();
                    const startTime = performance.now();

                    neutralizeClickTraps();
                    
                    document.querySelectorAll('div, section, aside, [role="dialog"], a, iframe, img, video').forEach(el => {
                        if (el.dataset.shielded || isSafeVisual(el)) return;
                        
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        
                        if (parseFloat(style.zIndex) > 500 && (style.position === 'fixed' || style.position === 'sticky')) {
                             const isLarge = rect.width > window.innerWidth * 0.4;
                             const isSus = el.className.includes('ad') || el.id.includes('ad') || el.onclick;
                             
                             if (isLarge || isSus) {
                                neutralize(el, "Potential Overlay");
                             }
                        }
                    });

                    // حماية المتصفح من التجمد
                    if (performance.now() - startTime > 50) {
                        console.log("[Shield] Throttled scan to prevent freeze");
                        window.__lastDOMScan += 2000;
                    }
                }

                // تخطي العدادات
                function bypassTimers() {
                    document.querySelectorAll('[class*="count" i], [id*="count" i]').forEach(el => {
                        const text = el.innerText || el.textContent;
                        if (/[0-9]/.test(text) && text.length < 10) el.innerText = "0";
                    });
                }

                // حماية الملاحة (Navigation Guard)
                const safeNav = (url) => (window.__userGesture || dlPatterns.some(p => url.toLowerCase().includes(p)));
                ['assign', 'replace'].forEach(m => {
                    const orig = window.location[m];
                    window.location[m] = function(url) {
                        if (!safeNav(url)) { console.log("[Shield] Blocked forced redirect: " + url); return; }
                        return orig.apply(window.location, arguments);
                    };
                });

                // نبضات النظام والمراقب (Watchdog)
                let last = Date.now();
                setInterval(() => {
                    if (Date.now() - last > 3000) console.log("[Shield] Heartbeat OK");
                    last = Date.now();
                    sanitizeParams();
                    cleanDOM();
                    bypassTimers();
                }, 1000);

                const obs = new MutationObserver(() => { if (!window.__userGesture) cleanDOM(); });
                obs.observe(document.documentElement, { childList: true, subtree: true });

                // إخفاء الأتمتة
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                console.log("Shield v4.0 Fully active");
            })();
            """
            await page.add_init_script(shield_script)
            
            # Renewal Overlay
            if target_url:
                 await page.add_init_script("""() => {
                    const div = document.createElement('div');
                    div.style = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.4);backdrop-filter:blur(2px);z-index:9999999;display:flex;align-items:center;justify-content:center;pointer-events:none;';
                    div.innerHTML = `
                        <div style="background:#2196F3;color:white;padding:20px 40px;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,0.3);text-align:center;">
                            <h2 style="margin:0 0 10px 0;font-family:sans-serif;">🟢 DLM is waiting for the download...</h2>
                            <p style="margin:0;opacity:0.9;">Please click the download button manually to refresh the session.</p>
                        </div>
                    `;
                    document.documentElement.appendChild(div);
                }""")
                 
            page.on("download", handle_download)

        # تهيئة التبويبات
        if context.pages:
            await setup_page(context.pages[0])
        context.on("page", setup_page)

        if target_url:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(target_url)

        print("[Browser] Ready. Navigate to any file to capture it.")
        
        exit_event = asyncio.Event()
        context.on("close", lambda _: exit_event.set())
        
        try:
            await exit_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await context.close()
            import shutil
            shutil.rmtree(current_profile, ignore_errors=True)
            print("\n[Browser] Closed.")

# ملاحظات حول تطوير نظام التحميل مستقبلاً:
# يمكن تحديث 'DownloadService.start_download' لاستخدام 'storage_state' من جدول التحميلات
# لاسترجاع الكوكيز والهيدرز عند بدء التحميل الفعلي من قائمة الانتظار.
