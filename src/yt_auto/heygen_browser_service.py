import os
import hashlib
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path


try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception:  # Playwright is optional until HeyGen automation is enabled.
    PlaywrightTimeoutError = Exception
    sync_playwright = None


class HeyGenBrowserService:
    def __init__(self, config, output_dir):
        self.config = config or {}
        self.output_dir = output_dir
        self.download_dir = self._ensure_dir(self.config.get("download_dir") or os.path.join(output_dir, "heygen_downloads"))
        self.debug_dir = self._ensure_dir(os.path.join(output_dir, "heygen_debug"))

    def _ensure_dir(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)
        return str(Path(path))

    def _safe_output_name(self, task_id, cut_key, suffix=".mp4"):
        suffix = suffix if str(suffix or "").startswith(".") else f".{suffix}"
        base = f"{task_id}_{cut_key}_heygen{suffix}"
        base = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", base).strip().strip(".")
        if len(base) <= 110:
            return base
        stem, ext = os.path.splitext(base)
        digest = hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:10]
        max_stem = max(20, 110 - len(ext) - len(digest) - 1)
        return f"{stem[:max_stem].rstrip(' ._')}_{digest}{ext}"

    def _truthy(self, value):
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def _launch_context(self, playwright):
        user_data_dir = self.config.get("chrome_user_data_dir") or os.path.join(self.output_dir, "heygen_chrome_profile")
        self._ensure_dir(user_data_dir)
        args = [
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
        ]
        extension_path = str(self.config.get("extension_path") or "").strip().strip('"')
        use_extension = self._truthy(self.config.get("use_streamvault_extension")) or self._truthy(self.config.get("allow_streamvault_extension_fallback"))
        if use_extension and extension_path and Path(extension_path).exists():
            args.extend([
                f"--disable-extensions-except={extension_path}",
                f"--load-extension={extension_path}",
            ])
        profile_directory = str(self.config.get("chrome_profile_directory") or "").strip().strip('"')
        if profile_directory:
            args.append(f"--profile-directory={profile_directory}")
        launch_kwargs = {
            "headless": self._truthy(self.config.get("headless")),
            "accept_downloads": True,
            "downloads_path": self.download_dir,
            "args": args,
            "viewport": {"width": 1920, "height": 1080},
        }
        executable_path = str(self.config.get("chrome_executable_path") or "").strip().strip('"')
        if executable_path and Path(executable_path).exists():
            launch_kwargs["executable_path"] = executable_path
        return playwright.chromium.launch_persistent_context(user_data_dir, **launch_kwargs)

    def _streamvault_workers(self, context):
        return [
            worker for worker in context.service_workers
            if "chrome-extension://" in worker.url and worker.url.endswith("/background.js")
        ]

    def _wait_for_streamvault_worker(self, context, timeout_ms=15000):
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            workers = self._streamvault_workers(context)
            if workers:
                return workers[0]
            time.sleep(0.5)
        return None

    def _downloads_search(self, worker, since_ms):
        try:
            return worker.evaluate(
                """(sinceMs) => new Promise((resolve) => {
                    chrome.downloads.search({ startedAfter: new Date(sinceMs).toISOString(), limit: 20 }, resolve);
                })""",
                since_ms,
            )
        except Exception:
            return []

    def _is_streamvault_video_download(self, item):
        path = Path(item.get("filename") or "")
        name = path.name.lower()
        if not item.get("filename") or item.get("state") != "complete":
            return False
        if name.startswith(("appear_", "disappear_", "transcode")):
            return False
        suffix = path.suffix.lower()
        return suffix in {"", ".mp4", ".webm", ".mov", ".m4v"}

    def _streamvault_media(self, worker, tab_id):
        try:
            response = worker.evaluate(
                """(tabId) => new Promise((resolve) => {
                    chrome.runtime.sendMessage({ type: 'GET_MEDIA', tabId }, (resp) => {
                        resolve({ resp, error: chrome.runtime.lastError ? chrome.runtime.lastError.message : '' });
                    });
                })""",
                tab_id,
            )
            if response.get("error"):
                return []
            return (response.get("resp") or {}).get("media") or []
        except Exception:
            return []

    def _streamvault_download_media(self, worker, item, tab_id):
        try:
            return worker.evaluate(
                """({ item, tabId }) => new Promise((resolve) => {
                    const filename = item.filename || ('heygen_' + Date.now() + '.mp4');
                    if (item.isHLS && typeof mergeHLS === 'function') {
                        mergeHLS(item.url, filename, tabId)
                            .then((name) => resolve({ resp: { success: true, filename: name }, error: '' }))
                            .catch((error) => resolve({ resp: { success: false, error: error.message }, error: '' }));
                        return;
                    }
                    chrome.downloads.download({ url: item.url, filename }, (downloadId) => {
                        resolve({
                            resp: { success: !chrome.runtime.lastError, downloadId },
                            error: chrome.runtime.lastError ? chrome.runtime.lastError.message : ''
                        });
                    });
                })""",
                {"item": item, "tabId": tab_id},
            )
        except Exception as error:
            return {"error": str(error)}

    def _first_visible(self, page, labels, timeout=5000):
        for label in labels:
            locator = page.get_by_text(label, exact=False)
            try:
                locator.first.wait_for(state="visible", timeout=timeout)
                return locator.first
            except Exception:
                continue
        return None

    def _click_first(self, page, labels, timeout=7000):
        locator = self._first_visible(page, labels, timeout=timeout)
        if locator:
            locator.click()
            return True
        return False

    def _click_first_force(self, page, labels, timeout=7000):
        locator = self._first_visible(page, labels, timeout=timeout)
        if locator:
            try:
                locator.click(force=True, timeout=timeout)
                return True
            except Exception:
                return False
        return False


    def _click_exact_text_node(self, page, text):
        try:
            return bool(page.evaluate(
                """(text) => {
                    const wanted = String(text || '').trim().toLowerCase();
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 2 && rect.height > 2 && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    let node;
                    while ((node = walker.nextNode())) {
                        const value = (node.nodeValue || '').trim().toLowerCase();
                        if (value !== wanted) continue;
                        let el = node.parentElement;
                        for (let i = 0; i < 6 && el; i += 1, el = el.parentElement) {
                            if (!visible(el)) continue;
                            const rect = el.getBoundingClientRect();
                            const target = el.closest('button,a,[role="button"]') || el;
                            target.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 }));
                            target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 }));
                            target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 }));
                            target.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                text,
            ))
        except Exception:
            return False

    def _click_text_js(self, page, patterns):
        try:
            return bool(page.evaluate(
                """(patterns) => {
                    const tests = patterns.map((pattern) => new RegExp(pattern, 'i'));
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 4 && rect.height > 4 && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const nodes = Array.from(document.querySelectorAll('button,a,[role="button"],div,span,p'))
                        .filter(visible)
                        .filter((el) => {
                            const text = (el.innerText || el.textContent || '').trim();
                            return text && tests.some((test) => test.test(text));
                        })
                        .sort((a, b) => {
                            const ar = a.getBoundingClientRect();
                            const br = b.getBoundingClientRect();
                            return (ar.top - br.top) || (ar.left - br.left);
                        });
                    const node = nodes[0];
                    if (!node) return false;
                    let target = node.closest('button,a,[role="button"]') || node;
                    target.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
                    target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                    target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                    target.click();
                    return true;
                }""",
                patterns,
            ))
        except Exception:
            return False

    def _is_login_screen(self, page):
        if "login" in page.url.lower() or "signin" in page.url.lower():
            return True
        try:
            body = page.evaluate("() => (document.body.innerText || '').toLowerCase()")
            if any(token in body for token in (
                "use email",
                "use password",
                "send a secure magic link",
                "continue with google",
                "sign in with google",
                "two-factor authentication",
            )):
                return True
        except Exception:
            pass
        return bool(self._first_visible(
            page,
            [
                "Log in",
                "Sign in",
                "Verify you are human",
                "Send a secure magic link",
                "Use password",
                "Use email",
                "Continue with Google",
                "Sign in with Google",
            ],
            timeout=1000,
        ))

    def _wait_for_manual_login(self, page, avatar_url):
        if self._auto_login(page, avatar_url):
            return

        should_wait_for_login = self._truthy(self.config.get("wait_for_login")) or not self._truthy(self.config.get("headless"))
        if not should_wait_for_login:
            raise RuntimeError("HeyGen is not logged in. Login once in the opened browser profile, then run HeyGen clips again.")

        deadline = time.time() + int(float(self.config.get("login_timeout_seconds") or 900))
        print("HEYGEN_LOGIN_REQUIRED: Please login in the opened browser window. Waiting...")
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            if not self._is_login_screen(page):
                page.goto(avatar_url, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(1800)
                if not self._is_login_screen(page):
                    print("HEYGEN_LOGIN_DONE: Continuing HeyGen smoke test.")
                    return
        raise RuntimeError("Timed out waiting for HeyGen login in the opened browser window.")

    def _auto_login(self, page, avatar_url):
        email = str(self.config.get("login_email") or os.environ.get("HEYGEN_EMAIL") or "").strip()
        password = str(self.config.get("login_password") or os.environ.get("HEYGEN_PASSWORD") or "").strip()
        if not email or not password:
            return False

        print("HEYGEN_AUTO_LOGIN: Signing in with the provided account...")
        try:
            if "login" not in page.url.lower() and "signin" not in page.url.lower():
                page.goto("https://app.heygen.com/login", wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(1500)

            has_visible_email = page.evaluate(
                """() => Array.from(document.querySelectorAll("input[type='email'], input[name*='email' i], input[placeholder*='email' i]"))
                    .some((el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 30 && rect.height > 10 && style.display !== 'none' && style.visibility !== 'hidden';
                    })"""
            )
            if not has_visible_email:
                clicked_email = False
                for _ in range(6):
                    clicked_email = self._click_exact_text_node(page, "Use email")
                    if not clicked_email:
                        clicked_email = self._click_text_js(page, [r"^\s*use\s+email\s*$", r"use\s+email", r"continue\s+with\s+email", r"sign\s+in\s+with\s+email"])
                    if not clicked_email:
                        try:
                            page.get_by_text("Use email", exact=True).click(timeout=1500, force=True)
                            clicked_email = True
                        except Exception:
                            pass
                    page.wait_for_timeout(1000)
                    has_visible_email = page.evaluate(
                        """() => Array.from(document.querySelectorAll("input[type='email'], input[name*='email' i], input[placeholder*='email' i]"))
                            .some((el) => {
                                const rect = el.getBoundingClientRect();
                                const style = window.getComputedStyle(el);
                                return rect.width > 30 && rect.height > 10 && style.display !== 'none' && style.visibility !== 'hidden';
                            })"""
                    )
                    if has_visible_email:
                        break
                page.wait_for_timeout(800)

            email_inputs = [
                page.locator("input[type='email']").first,
                page.locator("input[name*='email' i]").first,
                page.locator("input[placeholder*='email' i]").first,
                page.locator("input[type='text']").first,
                page.locator("input:not([type='password'])").first,
                page.get_by_placeholder("Enter your email").first,
                page.get_by_placeholder("Email").first,
            ]
            email_filled = False
            for field in email_inputs:
                try:
                    field.fill(email, timeout=5000)
                    email_filled = True
                    break
                except Exception:
                    continue
            if not email_filled:
                print("HEYGEN_AUTO_LOGIN_FAILED: Could not find the email input.")
                return False

            # HeyGen currently shows a magic-link form first. After the email is
            # filled, switch that same modal into password mode.
            if not self._click_first_force(page, ["Use password", "Log in with password", "Sign in with password"], timeout=5000):
                self._click_text_js(page, [r"use\\s+password", r"log\\s+in\\s+with\\s+password", r"sign\\s+in\\s+with\\s+password"])
            page.wait_for_timeout(1200)
            if page.locator("input[type='password']").count() == 0:
                self._click_first_force(page, ["Continue", "Next"], timeout=3000)
                page.wait_for_timeout(1200)
                if not self._click_first_force(page, ["Use password", "Log in with password", "Sign in with password"], timeout=3000):
                    self._click_text_js(page, [r"use\\s+password", r"log\\s+in\\s+with\\s+password", r"sign\\s+in\\s+with\\s+password"])
                page.wait_for_timeout(1000)

            password_inputs = [
                page.locator("input[type='password']").first,
                page.locator("input[name*='password' i]").first,
                page.locator("input[placeholder*='password' i]").first,
                page.get_by_placeholder("Password").first,
                page.get_by_placeholder("Enter your password").first,
            ]
            password_filled = False
            for field in password_inputs:
                try:
                    field.fill(password, timeout=5000)
                    password_filled = True
                    break
                except Exception:
                    continue
            if not password_filled:
                print("HEYGEN_AUTO_LOGIN_FAILED: Could not find the password input.")
                return False

            if not self._click_first_force(page, ["Log in", "Login", "Sign in", "Continue"], timeout=5000):
                try:
                    page.keyboard.press("Enter")
                except Exception:
                    pass

            deadline = time.time() + 90
            while time.time() < deadline:
                page.wait_for_timeout(2500)
                if not self._is_login_screen(page):
                    page.goto(avatar_url, wait_until="domcontentloaded", timeout=90000)
                    page.wait_for_timeout(1800)
                    if not self._is_login_screen(page):
                        print("HEYGEN_AUTO_LOGIN_DONE: Continuing HeyGen smoke test.")
                        return True
            print("HEYGEN_AUTO_LOGIN_FAILED: Login did not complete automatically.")
            return False
        except Exception as error:
            print(f"HEYGEN_AUTO_LOGIN_FAILED: {error}")
            return False

    def _debug_screenshot(self, page, task_id, cut_key):
        path = os.path.join(self.debug_dir, f"{task_id}_{cut_key}_heygen_debug.png")
        try:
            page.screenshot(path=path, full_page=True)
            return path
        except Exception:
            return None

    def _goto_with_retry(self, page, url, attempts=3):
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                return page.goto(url, wait_until="domcontentloaded", timeout=90000)
            except Exception as error:
                last_error = error
                if attempt >= attempts:
                    break
                print(f"HEYGEN_NAV_RETRY: attempt={attempt} error={error}", flush=True)
                page.wait_for_timeout(3000 * attempt)
        raise last_error

    def _dismiss_popups(self, page):
        try:
            plans = page.get_by_text("Plans that fit your scale", exact=False).first
            plans.wait_for(state="visible", timeout=1500)
            close_point = page.evaluate(
                """() => {
                    const nodes = Array.from(document.querySelectorAll('*'));
                    const heading = nodes.find(el => (el.textContent || '').trim() === 'Plans that fit your scale');
                    if (!heading) return null;
                    let el = heading;
                    for (let i = 0; i < 8 && el; i += 1) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 700 && rect.height > 500) {
                            return { x: rect.right - 42, y: rect.top + 32 };
                        }
                        el = el.parentElement;
                    }
                    const rect = heading.getBoundingClientRect();
                    return { x: rect.right + 590, y: Math.max(80, rect.top - 70) };
                }"""
            )
            if close_point:
                page.mouse.click(close_point["x"], close_point["y"])
                page.wait_for_timeout(800)
                return True
        except Exception:
            pass
        close_candidates = [
            page.locator("button[aria-label*='close' i]").last,
            page.locator("[role='dialog'] button").filter(has_text="Ã—").last,
            page.locator("[role='dialog'] svg").locator("xpath=ancestor::button").last,
            page.get_by_text("Maybe later", exact=False).first,
            page.get_by_text("Skip", exact=False).first,
        ]
        for candidate in close_candidates:
            try:
                candidate.click(timeout=2500, force=True)
                page.wait_for_timeout(800)
                return True
            except Exception:
                continue
        try:
            close_help = page.get_by_text("Send us a message", exact=False).first
            close_help.wait_for(state="visible", timeout=800)
            page.mouse.click(1845, 318)
            page.wait_for_timeout(500)
            return True
        except Exception:
            pass
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
            return True
        except Exception:
            return False

    def _avatar_selector_parts(self, avatar_name):
        if isinstance(avatar_name, dict):
            group_name = str(avatar_name.get("group") or avatar_name.get("avatar_group") or "").strip()
            look_name = str(avatar_name.get("look") or avatar_name.get("avatar_name") or avatar_name.get("name") or "").strip()
            label = " / ".join([part for part in (group_name, look_name) if part]) or str(avatar_name)
            return group_name, look_name, label
        text = str(avatar_name or "").strip()
        if " > " in text:
            group_name, look_name = [part.strip() for part in text.split(" > ", 1)]
            return group_name, look_name, text
        return "", text, text

    def _look_index_from_name(self, name):
        text = str(name or "").strip().lower()
        match = re.search(r"(?:look|frame|avatar|#)?\s*0*(\d+)\s*$", text)
        if not match:
            return None
        index = int(match.group(1))
        return index if index > 0 else None

    def _avatar_group_id_from_url(self, url):
        match = re.search(r"/avatar/my-avatars/([a-f0-9]{24,40})", str(url or ""), re.IGNORECASE)
        return match.group(1) if match else ""

    def _extract_look_ids_from_page(self, page):
        try:
            ids = page.evaluate(
                """() => {
                    const ids = [];
                    for (const img of Array.from(document.querySelectorAll('img'))) {
                        const src = String(img.getAttribute('src') || img.currentSrc || '');
                        const match = src.match(/\\/talking_photo\\/([a-f0-9]{24,40})\\//i);
                        if (match && !ids.includes(match[1])) ids.push(match[1]);
                    }
                    return ids;
                }"""
            )
            return [str(item).strip() for item in (ids or []) if str(item).strip()]
        except Exception:
            return []

    def _direct_create_url_for_exact_look(self, page, avatar_url, look_name):
        group_id = self._avatar_group_id_from_url(avatar_url) or self._avatar_group_id_from_url(page.url)
        if not group_id:
            return ""
        text = str(look_name or "").strip()
        direct_match = re.search(r"\b([a-f0-9]{24,40})\b", text, re.IGNORECASE)
        if direct_match:
            look_id = direct_match.group(1)
        else:
            look_index = self._look_index_from_name(text)
            if not look_index:
                return ""
            look_ids = self._extract_look_ids_from_page(page)
            if look_index > len(look_ids):
                raise RuntimeError(
                    f"HeyGen look {look_name!r} requested index {look_index}, but only {len(look_ids)} looks were visible."
                )
            look_id = look_ids[look_index - 1]
        return (
            "https://app.heygen.com/create-v4/draft"
            f"?vt=p&avatarGroup={group_id}&defaultLookId={look_id}"
            "&fromCreateButton=true&panel=scene"
        )

    def _choose_avatar(self, page, avatar_name):
        group_name, look_name, requested_label = self._avatar_selector_parts(avatar_name)

        def open_exact_card_action(name, action="menu"):
            return page.evaluate(
                """({ name, action }) => {
                    const normalize = (value) => String(value || '')
                        .normalize('NFKC')
                        .replace(/\s+/g, ' ')
                        .trim()
                        .toLowerCase();
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && Number(style.opacity || 1) > 0
                            && rect.width > 1
                            && rect.height > 1;
                    };
                    const needle = normalize(name);
                    const nodes = Array.from(document.querySelectorAll(
                        '[aria-label],[title],h1,h2,h3,h4,h5,h6,p,span,div'
                    )).filter(visible);
                    const exactNodes = nodes.filter((el) => {
                        const values = [
                            el.getAttribute('aria-label') || '',
                            el.getAttribute('title') || '',
                            ...(el.innerText || '').split(String.fromCharCode(10))
                        ];
                        return values.some((value) => normalize(value) === needle);
                    });
                    const cards = [];
                    for (const node of exactNodes) {
                        let card = node;
                        for (let depth = 0; card && depth < 9; depth += 1, card = card.parentElement) {
                            const rect = card.getBoundingClientRect();
                            const buttons = Array.from(card.querySelectorAll('button,[role="button"]'))
                                .filter(visible);
                            if (
                                rect.width >= 150
                                && rect.height >= 100
                                && rect.width <= Math.max(1100, window.innerWidth * 0.9)
                                && rect.height <= Math.max(900, window.innerHeight * 0.95)
                                && (buttons.length || action === 'open')
                            ) {
                                if (!cards.includes(card)) cards.push(card);
                                break;
                            }
                        }
                    }
                    if (!cards.length) {
                        const labels = Array.from(new Set(nodes.flatMap((el) =>
                            (el.innerText || '').split(String.fromCharCode(10)).map((line) => line.trim())
                        ).filter((line) => line && line.length <= 80))).slice(0, 40);
                        return { ok: false, reason: 'not_found', labels };
                    }
                    cards.sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return (ar.width * ar.height) - (br.width * br.height);
                    });
                    const card = cards[0];
                    card.scrollIntoView({ block: 'center', inline: 'center' });
                    card.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
                    if (action === 'open') {
                        const rect = card.getBoundingClientRect();
                        const target = card.closest('a,[role="button"],button') || card;
                        target.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
                        target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 }));
                        target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 }));
                        target.click();
                        return { ok: true, matched: name, action };
                    }
                    const buttons = Array.from(card.querySelectorAll('button,[role="button"]'))
                        .filter(visible);
                    const menu = buttons.find((button) => {
                        const label = normalize([
                            button.getAttribute('aria-label') || '',
                            button.getAttribute('title') || '',
                            button.innerText || ''
                        ].join(' '));
                        return label.includes('create video')
                            || label.includes('create')
                            || label.includes('more')
                            || label.includes('menu')
                            || label.includes('...');
                    }) || buttons[buttons.length - 1];
                    if (!menu) {
                        return { ok: false, reason: 'no_action_button', labels: [name] };
                    }
                    menu.click();
                    return { ok: true, matched: name, action };
                }""",
                {"name": name, "action": action},
            )

        def fill_search(name):
            try:
                search = page.get_by_placeholder("Search").first
                search.fill(name)
                page.wait_for_timeout(1200)
                return True
            except Exception:
                return False

        def select_named_card(name, action="menu", allow_manage=True):
            if not name:
                return None
            fill_search(name)
            result = open_exact_card_action(name, action)
            if result and result.get("ok"):
                print(f"HEYGEN_AVATAR_SELECTED requested={name!r} matched={result.get('matched')!r} action={action}", flush=True)
                return result

            if allow_manage:
                for manage_label in ("MANAGE", "Manage", "My avatars", "My Avatars"):
                    try:
                        page.get_by_text(manage_label, exact=True).last.click(timeout=3000, force=True)
                        page.wait_for_timeout(2200)
                        fill_search(name)
                        result = open_exact_card_action(name, action)
                        if result and result.get("ok"):
                            print(f"HEYGEN_AVATAR_SELECTED requested={name!r} matched={result.get('matched')!r} via={manage_label!r} action={action}", flush=True)
                            return result
                    except Exception:
                        continue
            return result

        def select_look_by_index(index, action="menu"):
            try:
                search = page.get_by_placeholder("Search").first
                search.fill("")
                page.wait_for_timeout(800)
            except Exception:
                pass
            return page.evaluate(
                """({ index, action }) => {
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && Number(style.opacity || 1) > 0
                            && rect.width > 80
                            && rect.height > 80;
                    };
                    const centerClick = (el) => {
                        const rect = el.getBoundingClientRect();
                        const target = el.closest('button,[role="button"],a') || el;
                        target.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 }));
                        target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 }));
                        target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 }));
                        target.click();
                    };
                    const candidates = [];
                    const nodes = Array.from(document.querySelectorAll('div,button,[role="button"],a'));
                    for (const node of nodes) {
                        if (!visible(node)) continue;
                        const text = (node.innerText || node.textContent || '').trim().toLowerCase();
                        const rect = node.getBoundingClientRect();
                        if (!text.includes('photo avatar')) continue;
                        if (text.includes('upload look') || text.includes('design with ai')) continue;
                        if (rect.left < window.innerWidth * 0.18 || rect.top < 160) continue;
                        if (rect.width < 120 || rect.height < 140 || rect.width > 420 || rect.height > 460) continue;
                        const imgCount = node.querySelectorAll('img,canvas,video,[style*="background-image"]').length;
                        if (!imgCount && !node.querySelector('[style*="background-image"]')) continue;
                        candidates.push({ el: node, rect, score: rect.width * rect.height });
                    }
                    const unique = [];
                    for (const item of candidates.sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left || a.score - b.score)) {
                        const overlaps = unique.some((seen) => {
                            const a = item.rect;
                            const b = seen.rect;
                            return Math.abs(a.left - b.left) < 16 && Math.abs(a.top - b.top) < 16;
                        });
                        if (!overlaps) unique.push(item);
                    }
                    if (!unique.length) {
                        const mediaNodes = Array.from(document.querySelectorAll('img,canvas,video,[style*="background-image"]'))
                            .filter(visible)
                            .map((el) => {
                                const rect = el.getBoundingClientRect();
                                let card = el;
                                for (let depth = 0; card && depth < 8; depth += 1, card = card.parentElement) {
                                    const cardRect = card.getBoundingClientRect();
                                    const text = (card.innerText || card.textContent || '').trim().toLowerCase();
                                    if (
                                        cardRect.left > window.innerWidth * 0.18
                                        && cardRect.top > 160
                                        && cardRect.width >= 120
                                        && cardRect.height >= 140
                                        && cardRect.width <= 440
                                        && cardRect.height <= 520
                                        && !text.includes('upload look')
                                        && !text.includes('design with ai')
                                    ) {
                                        return { el: card, rect: cardRect, score: cardRect.width * cardRect.height };
                                    }
                                }
                                return { el, rect, score: rect.width * rect.height };
                            })
                            .filter((item) => item.rect.left > window.innerWidth * 0.18 && item.rect.top > 160)
                            .filter((item) => item.rect.width >= 90 && item.rect.height >= 90)
                            .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left || a.score - b.score);
                        for (const item of mediaNodes) {
                            const overlaps = unique.some((seen) => {
                                const a = item.rect;
                                const b = seen.rect;
                                return Math.abs(a.left - b.left) < 24 && Math.abs(a.top - b.top) < 24;
                            });
                            if (!overlaps) unique.push(item);
                        }
                    }
                    const wanted = unique[index - 1];
                    if (!wanted) {
                        return { ok: false, reason: 'index_not_found', count: unique.length };
                    }
                    wanted.el.scrollIntoView({ block: 'center', inline: 'center' });
                    if (action === 'open') {
                        centerClick(wanted.el);
                        return { ok: true, index, count: unique.length, action };
                    }
                    const buttons = Array.from(wanted.el.querySelectorAll('button,[role="button"]'))
                        .filter((button) => {
                            const rect = button.getBoundingClientRect();
                            const style = window.getComputedStyle(button);
                            return rect.width > 1 && rect.height > 1 && style.display !== 'none' && style.visibility !== 'hidden';
                        });
                    const menu = buttons.find((button) => {
                        const label = String([
                            button.getAttribute('aria-label') || '',
                            button.getAttribute('title') || '',
                            button.innerText || ''
                        ].join(' ')).toLowerCase();
                        return label.includes('create video')
                            || label.includes('create')
                            || label.includes('more')
                            || label.includes('menu')
                            || label.includes('...');
                    }) || buttons[buttons.length - 1];
                    if (menu) {
                        menu.click();
                    } else {
                        centerClick(wanted.el);
                    }
                    return { ok: true, index, count: unique.length, action };
                }""",
                {"index": int(index), "action": action},
            )

        try:
            if group_name and look_name:
                already_on_looks_page = False
                try:
                    already_on_looks_page = bool(page.evaluate(
                        """() => /\\/avatar\\/my-avatars\\/[^/?#]+/.test(window.location.pathname)
                            || /\\b\\d+\\s+looks\\b/i.test(document.body.innerText || '')
                            || /upload\\s+look/i.test(document.body.innerText || '')"""
                    ))
                except Exception:
                    already_on_looks_page = False
                if not already_on_looks_page:
                    result = select_named_card(group_name, action="open", allow_manage=True)
                    if not result or not result.get("ok"):
                        labels = ", ".join((result or {}).get("labels", [])[:12])
                        reason = (result or {}).get("reason", "unknown")
                        raise RuntimeError(
                            f"Exact HeyGen avatar group {group_name!r} could not be opened "
                            f"({reason}). Visible names: {labels or 'none'}"
                        )
                    page.wait_for_timeout(2600)
                for _ in range(2):
                    self._dismiss_popups(page)
                    page.wait_for_timeout(400)
                look_index = self._look_index_from_name(look_name)
                if look_index:
                    result = select_look_by_index(look_index, action="menu")
                    if result and result.get("ok"):
                        print(
                            f"HEYGEN_AVATAR_LOOK_SELECTED group={group_name!r} look_index={look_index} count={result.get('count')}",
                            flush=True,
                        )
                        return
                result = select_named_card(look_name, action="menu", allow_manage=False)
                if result and result.get("ok"):
                    print(f"HEYGEN_AVATAR_LOOK_SELECTED group={group_name!r} look={look_name!r}", flush=True)
                    return
                labels = ", ".join((result or {}).get("labels", [])[:12])
                reason = (result or {}).get("reason", "unknown")
                raise RuntimeError(
                    f"Exact HeyGen avatar look {look_name!r} could not be selected inside group {group_name!r} "
                    f"({reason}). Visible names: {labels or 'none'}"
                )

            if look_name:
                result = select_named_card(look_name, action="menu", allow_manage=True)
                if result and result.get("ok"):
                    return
                labels = ", ".join((result or {}).get("labels", [])[:12])
                reason = (result or {}).get("reason", "unknown")
                raise RuntimeError(
                    f"Exact HeyGen avatar {look_name!r} could not be selected "
                    f"({reason}). Visible names: {labels or 'none'}"
                )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Exact HeyGen avatar {requested_label!r} could not be selected. Inner error: {type(exc).__name__}: {exc}"
            ) from exc

        # Only unnamed requests may use the first available avatar. A named
        # request must never silently fall back to the wrong presenter.
        try:
            first_card = page.locator("[role='button']").filter(has_text="look").first
            first_card.hover(timeout=5000)
            page.wait_for_timeout(500)
            create_button = page.locator("button[aria-label='Create video with this avatar']").first
            create_button.click(timeout=5000, force=True)
            return
        except Exception:
            pass
        menu_candidates = [
            page.locator("button[aria-label*='more' i]").first,
            page.locator("button:has-text('...')").first,
            page.locator("svg").locator("xpath=ancestor::button").first,
        ]
        for candidate in menu_candidates:
            try:
                candidate.click(timeout=3000)
                return
            except Exception:
                continue
        raise RuntimeError("Could not open a HeyGen avatar action menu. Set a valid HeyGen avatar name in admin settings.")


    def _open_look_card_actions_menu(self, page, avatar_name):
        _, look_name, requested_label = self._avatar_selector_parts(avatar_name)
        target = (look_name or requested_label or "").strip().lower()
        try:
            rect = page.evaluate(
                """(target) => {
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width > 80 && r.height > 80 && st.display !== 'none' && st.visibility !== 'hidden';
                    };
                    const cards = Array.from(document.querySelectorAll('div,[role="button"],button'))
                        .filter(visible)
                        .map((el) => ({ el, rect: el.getBoundingClientRect(), text: (el.innerText || '').trim().toLowerCase() }))
                        .filter((item) => item.text.includes(target) && item.rect.top > 150 && item.rect.left < window.innerWidth * 0.75);
                    cards.sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));
                    const card = cards[0];
                    if (!card) return null;
                    return { left: card.rect.left, top: card.rect.top, width: card.rect.width, height: card.rect.height };
                }""",
                target,
            )
            if not rect:
                return False
            page.mouse.move(rect["left"] + rect["width"] * 0.78, rect["top"] + 24)
            page.wait_for_timeout(900)
            opened = page.evaluate(
                """(rect) => {
                    const buttons = Array.from(document.querySelectorAll('button,[role="button"]'))
                        .map((button) => ({ button, r: button.getBoundingClientRect(), label: [button.innerText || '', button.getAttribute('aria-label') || '', button.getAttribute('title') || ''].join(' ').trim() }))
                        .filter((item) => item.r.width > 16 && item.r.height > 16)
                        .filter((item) => item.r.left >= rect.left + rect.width * 0.50 && item.r.left <= rect.left + rect.width + 12 && item.r.top >= rect.top - 8 && item.r.top <= rect.top + 58)
                        .sort((a, b) => a.r.left - b.r.left);
                    const preferred = buttons.find((item) => /create|video|build|script/i.test(item.label)) || (buttons.length >= 3 ? buttons[1] : buttons[0]);
                    if (!preferred) return { ok: false, count: buttons.length };
                    preferred.button.click();
                    return { ok: true, count: buttons.length, label: preferred.label };
                }""",
                rect,
            )
            if opened and opened.get("ok"):
                print(f"HEYGEN_LOOK_CARD_MENU: opened action label={opened.get('label')!r} count={opened.get('count')}", flush=True)
                page.wait_for_timeout(800)
                return True
        except Exception as exc:
            print(f"HEYGEN_LOOK_CARD_MENU: failed {type(exc).__name__}: {exc}", flush=True)
        return False

    def _open_avatar_actions_menu(self, page):
        try:
            if page.evaluate(
                """() => {
                    const buttons = Array.from(document.querySelectorAll('button,[role="button"]'))
                        .filter((button) => {
                            const rect = button.getBoundingClientRect();
                            if (rect.width < 12 || rect.height < 12) return false;
                            if (rect.top > 220 || rect.left < window.innerWidth * 0.55) return false;
                            if (rect.width <= 70 && rect.height <= 70 && rect.left > window.innerWidth * 0.78 && rect.left < window.innerWidth * 0.98) return true;
                            const label = [
                                button.getAttribute('aria-label') || '',
                                button.getAttribute('title') || '',
                                button.innerText || ''
                            ].join(' ').toLowerCase();
                            return label.includes('more') || label.includes('menu') || label.includes('...');
                        })
                        .sort((a, b) => {
                            const target = window.innerWidth * 0.91;
                            const ar = a.getBoundingClientRect();
                            const br = b.getBoundingClientRect();
                            return Math.abs((ar.left + ar.width / 2) - target) - Math.abs((br.left + br.width / 2) - target);
                        });
                    const menu = buttons[0];
                    if (!menu) return false;
                    menu.click();
                    return true;
                }"""
            ):
                page.wait_for_timeout(500)
                return True
        except Exception:
            pass
        try:
            more = page.locator("button[aria-label*='more' i]").last
            more.click(timeout=3000, force=True)
            page.wait_for_timeout(500)
            return True
        except Exception:
            return False

    def _open_scene_from_avatar_detail(self, page):
        try:
            result = page.evaluate(
                """() => {
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 10 && rect.height > 10 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const buttons = Array.from(document.querySelectorAll('button,[role="button"]'))
                        .filter(visible)
                        .map((button) => {
                            const label = [
                                button.innerText || '',
                                button.getAttribute('aria-label') || '',
                                button.getAttribute('title') || ''
                            ].join(' ').trim();
                            return { button, label };
                        });
                    const useInVideo = buttons.find((item) => /use\\s+in\\s+video|use\\s+this\\s+avatar|video\\s+with\\s+this\\s+avatar/i.test(item.label));
                    if (!useInVideo) return { ok: false };
                    useInVideo.button.click();
                    return { ok: true, label: useInVideo.label };
                }"""
            )
            if result and result.get("ok"):
                print(f"HEYGEN_SCENE_BUILDER: avatar detail modal clicked label={result.get('label')!r}", flush=True)
                page.wait_for_timeout(2500)
                return True
        except Exception:
            pass
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass
        try:
            result = page.evaluate(
                """() => {
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 10 && rect.height > 10 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const buttons = Array.from(document.querySelectorAll('button,[role="button"]'))
                        .filter(visible)
                        .map((button) => {
                            const rect = button.getBoundingClientRect();
                            const label = [
                                button.innerText || '',
                                button.getAttribute('aria-label') || '',
                                button.getAttribute('title') || ''
                            ].join(' ').trim();
                            return { button, rect, label };
                        });
                    const named = buttons.find((item) => {
                        const label = item.label.toLowerCase();
                        return (
                            label.includes('build scene')
                            || label.includes('create video')
                            || label === 'create'
                            || label.includes('use this avatar')
                            || label.includes('use in video')
                            || label.includes('video with this avatar')
                        ) && !label.includes('new avatar');
                    });
                    if (named) {
                        named.button.click();
                        return { ok: true, via: 'named', label: named.label };
                    }
                    return { ok: false };
                }"""
            )
            if result and result.get("ok"):
                print(f"HEYGEN_SCENE_BUILDER: avatar detail create clicked via {result.get('via')}", flush=True)
                page.wait_for_timeout(2500)
                return True
        except Exception:
            pass
        return False

    def _open_scene_builder(self, page, avatar_name):
        def is_scene_editor():
            try:
                return bool(self._first_visible(page, ["Upload audio", "Upload Audio", "Render Scene", "Avatar & Voice", "Scene rendered", "Generate"], timeout=1200))
            except Exception:
                return False

        avatar_url = self.config.get("avatar_page_url") or "https://app.heygen.com/avatar/my-avatars"
        self._goto_with_retry(page, avatar_url)
        page.wait_for_timeout(1800)
        if self._is_login_screen(page):
            self._wait_for_manual_login(page, avatar_url)
        for _ in range(3):
            if not self._dismiss_popups(page):
                break
            page.wait_for_timeout(600)

        group_name, look_name, _ = self._avatar_selector_parts(avatar_name)
        if group_name and look_name and self._avatar_group_id_from_url(avatar_url):
            direct_url = self._direct_create_url_for_exact_look(page, avatar_url, look_name)
            if direct_url:
                print(f"HEYGEN_SCENE_BUILDER: opening exact look directly {look_name!r}", flush=True)
                self._goto_with_retry(page, direct_url)
                page.wait_for_load_state("domcontentloaded", timeout=90000)
                deadline = time.time() + 90
                while time.time() < deadline:
                    if is_scene_editor():
                        print("HEYGEN_SCENE_BUILDER: exact look scene editor opened", flush=True)
                        return
                    page.wait_for_timeout(1000)

        editor_deadline = time.time() + (45 if "/create" in str(avatar_url).lower() else 5)
        while time.time() < editor_deadline:
            if is_scene_editor():
                print("HEYGEN_SCENE_BUILDER: already in scene editor", flush=True)
                return
            page.wait_for_timeout(1500)

        if self._is_login_screen(page):
            self._wait_for_manual_login(page, avatar_url)
            for _ in range(3):
                if not self._dismiss_popups(page):
                    break
                page.wait_for_timeout(600)
            if is_scene_editor():
                print("HEYGEN_SCENE_BUILDER: scene editor opened after login", flush=True)
                return

        self._choose_avatar(page, avatar_name)
        page.wait_for_timeout(600)
        if is_scene_editor():
            print("HEYGEN_SCENE_BUILDER: scene editor opened from avatar", flush=True)
            return

        build_labels = ["CREATE VIDEO", "Create video", "Create Video", "Avatar video", "Create avatar video", "Build scene-by-scene", "Build scene by scene", "Build scene", "Use this avatar", "Use in video", "Video with this avatar"]
        print("HEYGEN_SCENE_BUILDER: looking for build action", flush=True)
        if not self._click_first_force(page, build_labels, timeout=4000):
            if is_scene_editor():
                print("HEYGEN_SCENE_BUILDER: scene editor opened during build click", flush=True)
                return
            print("HEYGEN_SCENE_BUILDER: opening avatar actions menu", flush=True)
            if not self._open_look_card_actions_menu(page, avatar_name):
                self._open_avatar_actions_menu(page)
            page.wait_for_timeout(600)
        if not self._click_first_force(page, build_labels, timeout=7000):
            if is_scene_editor():
                print("HEYGEN_SCENE_BUILDER: scene editor opened after build menu", flush=True)
                return
            if self._open_scene_from_avatar_detail(page):
                deadline = time.time() + 15
                while time.time() < deadline:
                    if is_scene_editor():
                        print("HEYGEN_SCENE_BUILDER: scene editor opened from avatar detail", flush=True)
                        return
                    page.wait_for_timeout(1000)
                if self._click_first_force(page, build_labels, timeout=5000):
                    print("HEYGEN_SCENE_BUILDER: build action clicked from avatar detail", flush=True)
                    page.wait_for_load_state("domcontentloaded", timeout=90000)
                    deadline = time.time() + 90
                    while time.time() < deadline:
                        if is_scene_editor():
                            return
                        page.wait_for_timeout(1000)
            raise RuntimeError("Could not find HeyGen 'Build scene-by-scene' action for the selected avatar.")
        print("HEYGEN_SCENE_BUILDER: build action clicked", flush=True)
        page.wait_for_load_state("domcontentloaded", timeout=90000)
        page.wait_for_timeout(2500)

    def _heygen_audio_attached(self, page, audio_path):
        try:
            return bool(page.evaluate(
                """() => {
                    const text = (document.body.innerText || '').toLowerCase();
                    if (text.includes('recorded voice')) return true;
                    if (/00:00\\s*\\/\\s*(?:00:[1-5][0-9]|0[1-9]:[0-5][0-9]|[1-9][0-9]:[0-5][0-9])/.test(text)) return true;
                    return false;
                }""",
            ))
        except Exception:
            return False

    def _click_add_audio_confirmation(self, page):
        if self._click_first_force(page, ["Add audio", "Add Audio"], timeout=2500):
            page.wait_for_timeout(1500)
            return True
        try:
            return bool(page.evaluate(
                """() => {
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 10 && rect.height > 10 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const nodes = Array.from(document.querySelectorAll('button,[role="button"]'))
                        .filter(visible)
                        .filter((el) => /\\badd\\s+audio\\b/i.test([
                            el.innerText || '',
                            el.getAttribute('aria-label') || '',
                            el.getAttribute('title') || ''
                        ].join(' ')));
                    if (!nodes.length) return false;
                    nodes[0].click();
                    return true;
                }"""
            ))
        except Exception:
            return False

    def _set_heygen_audio_file(self, page, audio_path):
        inputs = page.locator("input[type='file']")
        try:
            count = inputs.count()
        except Exception:
            count = 0
        for index in range(count - 1, -1, -1):
            try:
                inputs.nth(index).set_input_files(audio_path, timeout=7000)
                return True
            except Exception:
                continue
        try:
            with page.expect_file_chooser(timeout=12000) as chooser_info:
                if not self._click_first_force(page, ["Upload a file", "Upload Audio", "Upload audio"], timeout=6000):
                    page.evaluate(
                        """() => {
                            const node = Array.from(document.querySelectorAll('button,[role="button"],div'))
                                .find((el) => /upload\\s+a\\s+file|upload\\s+audio/i.test(el.innerText || ''));
                            if (node) node.click();
                        }"""
                    )
            chooser_info.value.set_files(audio_path)
            return True
        except Exception:
            return False

    def _upload_audio(self, page, audio_path):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            pass

        for attempt in range(1, 4):
            if self._heygen_audio_attached(page, audio_path):
                return
            if not self._click_first_force(page, ["Upload audio", "Upload Audio"], timeout=12000):
                clicked = page.evaluate(
                    """() => {
                        const visible = (el) => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 10 && rect.height > 10 && style.visibility !== 'hidden' && style.display !== 'none';
                        };
                        const nodes = Array.from(document.querySelectorAll('button,[role="button"]'))
                            .filter(visible)
                            .filter((el) => /upload\\s+audio/i.test([
                                el.innerText || '',
                                el.getAttribute('aria-label') || '',
                                el.getAttribute('title') || ''
                            ].join(' ')));
                        if (!nodes.length) return false;
                        nodes[0].click();
                        return true;
                    }"""
                )
                if not clicked:
                    raise RuntimeError("Could not find HeyGen Upload audio button.")
            page.wait_for_timeout(900)

            if not self._set_heygen_audio_file(page, audio_path):
                page.keyboard.press("Escape")
                page.wait_for_timeout(800)
                continue

            deadline = time.time() + 55
            while time.time() < deadline:
                if self._click_add_audio_confirmation(page):
                    page.wait_for_timeout(2500)
                    if self._heygen_audio_attached(page, audio_path):
                        return
                if self._heygen_audio_attached(page, audio_path):
                    return
                page.wait_for_timeout(1200)

            print(f"HEYGEN_UPLOAD: audio did not attach on attempt {attempt}, retrying", flush=True)
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(1000)
            except Exception:
                pass
        raise RuntimeError("Audio uploaded, but HeyGen did not show a clickable Add audio confirmation.")

    def _select_motion_engine(self, page):
        engine = str(self.config.get("motion_engine") or "Avatar III").strip()
        if not engine:
            return
        try:
            page.get_by_text("Motion Engine", exact=False).first.wait_for(timeout=7000)
        except Exception:
            return
        try:
            page.get_by_text("Avatar IV", exact=False).first.click(timeout=2500)
        except Exception:
            try:
                page.get_by_text("Motion Engine", exact=False).first.click(timeout=2500)
            except Exception:
                pass
        page.wait_for_timeout(500)
        try:
            page.get_by_text(engine, exact=False).last.click(timeout=5000)
        except Exception:
            pass

    def _install_heygen_capture_hooks(self, page):
        page.evaluate(
            """() => {
                if (window.__orvionHeygenCapture && window.__orvionHeygenCapture.installed) {
                    const state = window.__orvionHeygenCapture;
                    state.startedAt = Date.now();
                    state.startedPerfAt = performance.now();
                    state.urls = [];
                    state.events = [];
                    return true;
                }
                const BAD_RE = /(appear_v1|disappear_v1|transcode\\.mp3|thumbnail|\\/upload(?:\\?|$|\\/)|\\.png(?:\\?|$)|\\.jpe?g(?:\\?|$)|\\.svg(?:\\?|$)|\\.css(?:\\?|$)|\\.js(?:\\?|$)|favicon)/i;
                const GOOD_RE = /\\.mp4(?:\\?|$)|\\.mov(?:\\?|$)|\\.webm(?:\\?|$)|\\/(?:video|media|render|stream|download|asset)\\//i;
                const state = {
                    installed: true,
                    startedAt: Date.now(),
                    startedPerfAt: performance.now(),
                    urls: [],
                    events: []
                };
                window.__orvionHeygenCapture = state;

                function add(url, source, meta) {
                    try {
                        if (!url || typeof url !== 'string') return;
                        if (BAD_RE.test(url)) return;
                        if (!GOOD_RE.test(url)) return;
                        if (meta && Number.isFinite(meta.perfStartTime)
                                && meta.perfStartTime < state.startedPerfAt - 50) return;
                        const existing = state.urls.find((item) => item.url === url);
                        const record = Object.assign({ url, source, ts: Date.now() }, meta || {});
                        if (existing) {
                            Object.assign(existing, record);
                        } else {
                            state.urls.push(record);
                        }
                    } catch (_) {}
                }
                state.add = add;

                const originalFetch = window.fetch;
                window.fetch = function(input, init) {
                    const requestUrl = typeof input === 'string' ? input : (input && input.url);
                    add(requestUrl, 'fetch-request');
                    return originalFetch.apply(this, arguments).then((response) => {
                        add(response.url || requestUrl, 'fetch-response', {
                            status: response.status,
                            contentType: response.headers ? (response.headers.get('content-type') || '') : ''
                        });
                        return response;
                    });
                };

                const originalOpen = XMLHttpRequest.prototype.open;
                XMLHttpRequest.prototype.open = function(method, url) {
                    this.__orvionHeygenUrl = url;
                    add(url, 'xhr-open');
                    return originalOpen.apply(this, arguments);
                };

                const originalSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.send = function() {
                    this.addEventListener('load', () => {
                        add(this.responseURL || this.__orvionHeygenUrl, 'xhr-load', {
                            status: this.status,
                            contentType: this.getResponseHeader ? (this.getResponseHeader('content-type') || '') : ''
                        });
                    });
                    return originalSend.apply(this, arguments);
                };

                try {
                    const observer = new PerformanceObserver((list) => {
                        for (const entry of list.getEntries()) {
                            add(entry.name, 'perf', {
                                perfStartTime: entry.startTime || 0,
                                duration: entry.duration || 0,
                                transferSize: entry.transferSize || 0,
                                encodedBodySize: entry.encodedBodySize || 0
                            });
                        }
                    });
                    observer.observe({ entryTypes: ['resource'] });
                    state.observer = observer;
                } catch (_) {}

                 return true;
            }"""
        )

    def _click_heygen_render_scene(self, page):
        result = page.evaluate(
            """() => {
                const patterns = [/^\\s*render\\s+scene\\s*$/i, /render\\s+scene/i];
                const visible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 10 && rect.height > 10 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const clickableAncestor = (el) => {
                    let current = el;
                    for (let i = 0; i < 10 && current; i += 1, current = current.parentElement) {
                        if (!visible(current)) continue;
                        if (current.matches && current.matches('button,[role="button"]')) return current;
                        const style = window.getComputedStyle(current);
                        if ((current.onclick || style.cursor === 'pointer') && current.getBoundingClientRect().width > 60) return current;
                    }
                    return null;
                };

                const candidates = Array.from(document.querySelectorAll('button,[role="button"]'))
                    .filter((el) => visible(el))
                    .map((el) => ({
                        el,
                        text: [
                            el.innerText || '',
                            el.getAttribute('aria-label') || '',
                            el.getAttribute('title') || ''
                        ].join(' ').trim()
                    }))
                    .filter((item) => patterns.some((pattern) => pattern.test(item.text)))
                    .filter((item) => !/generate\\s*$/i.test(item.text));
                if (candidates.length) {
                    candidates[0].el.click();
                    return { ok: true, via: 'button', text: candidates[0].text };
                }

                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walker.nextNode())) {
                    const text = (node.nodeValue || '').trim();
                    if (!patterns.some((pattern) => pattern.test(text))) continue;
                    const button = clickableAncestor(node.parentElement);
                    if (button) {
                        button.click();
                        return { ok: true, via: 'text', text };
                    }
                }
                return { ok: false, text: (document.body.innerText || '').slice(0, 1200) };
            }"""
        )
        if not result or not result.get("ok"):
            result = page.evaluate(
                """() => {
                    const patterns = [/^\s*generate\s*$/i, /generate\s+video/i];
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 10 && rect.height > 10 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const buttons = Array.from(document.querySelectorAll('button,[role="button"]'))
                        .filter(visible)
                        .map((el) => ({
                            el,
                            text: [el.innerText || '', el.getAttribute('aria-label') || '', el.getAttribute('title') || ''].join(' ').trim()
                        }))
                        .filter((item) => patterns.some((pattern) => pattern.test(item.text)));
                    if (!buttons.length) return { ok: false };
                    buttons[0].el.click();
                    return { ok: true, via: 'generate-button', text: buttons[0].text };
                }"""
            )
        if not result or not result.get("ok"):
            raise RuntimeError("Could not find HeyGen Render Scene or Generate button.")
        print(f"HEYGEN_CAPTURE_RENDER_CLICKED: {result.get('via')} {result.get('text')}", flush=True)

    def _submit_generate_modal_if_present(self, page):
        try:
            result = page.evaluate(
                """() => {
                    const bodyText = (document.body.innerText || '').toLowerCase();
                    if (!bodyText.includes('generate video')) return { ok: false, reason: 'no-modal' };
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 10 && rect.height > 10 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const buttons = Array.from(document.querySelectorAll('button,[role="button"]'))
                        .filter(visible)
                        .map((el) => ({
                            el,
                            text: [el.innerText || '', el.getAttribute('aria-label') || '', el.getAttribute('title') || ''].join(' ').trim()
                        }));
                    const submit = buttons.find((item) => /^submit$/i.test(item.text)) || buttons.find((item) => /generate|submit/i.test(item.text));
                    if (!submit) return { ok: false, reason: 'submit-not-found', text: bodyText.slice(0, 500) };
                    submit.el.click();
                    return { ok: true, text: submit.text };
                }"""
            )
            if result and result.get("ok"):
                print(f"HEYGEN_GENERATE_MODAL_SUBMITTED: {result.get('text')}", flush=True)
                page.wait_for_timeout(3000)
                return True
        except Exception as exc:
            print(f"HEYGEN_GENERATE_MODAL_SUBMIT_FAILED: {type(exc).__name__}: {exc}", flush=True)
        return False

    def _heygen_blocking_modal_reason(self, page):
        try:
            text = str(page.evaluate("() => (document.body.innerText || '').replace(/\\s+/g, ' ').toLowerCase()") or "")
        except Exception:
            return ""
        if "upgrade to unlock" in text and "available: 0" in text:
            return "HeyGen quota is 0; render requires upgrade or more free quota."
        if "some scenes don't have a script" in text and "submit" in text:
            return "HeyGen generate modal is waiting for Submit."
        return ""

    def _render_started_signal(self, page):
        try:
            return bool(page.evaluate(
                """() => {
                    const text = (document.body.innerText || '').toLowerCase();
                    if (text.includes('rendering') || text.includes('generating') || text.includes('render started')) return true;
                    const buttons = Array.from(document.querySelectorAll('button,[role="button"]'));
                    return buttons.some((button) => {
                        const label = [
                            button.innerText || '',
                            button.getAttribute('aria-label') || '',
                            button.getAttribute('title') || ''
                        ].join(' ').toLowerCase();
                        return (label.includes('cancel') || label.includes('rendering')) && button.getBoundingClientRect().width > 10;
                    });
                }"""
            ))
        except Exception:
            return False

    def _captured_heygen_urls(self, page):
        data = page.evaluate(
            """() => {
                const state = window.__orvionHeygenCapture || { urls: [] };
                try {
                    if (state.add) {
                        for (const entry of performance.getEntriesByType('resource')) {
                            state.add(entry.name, 'perf-poll', {
                                perfStartTime: entry.startTime || 0,
                                duration: entry.duration || 0,
                                transferSize: entry.transferSize || 0,
                                encodedBodySize: entry.encodedBodySize || 0
                            });
                        }
                    }
                } catch (_) {}
                return {
                    urls: (state.urls || []).slice(-100),
                    bodyText: (document.body.innerText || '').slice(0, 2000)
                };
            }"""
        )
        return data or {"urls": [], "bodyText": ""}

    def _score_heygen_url(self, item):
        url = str(item.get("url") or "")
        lower = url.lower()
        content_type = str(item.get("contentType") or "").lower()
        if not url:
            return -1000
        if any(token in lower for token in ("appear_v1", "disappear_v1", "transcode.mp3", ".m3u8", "thumbnail", "/upload")):
            return -1000
        score = 0
        if ".mp4" in lower:
            score += 80
        if ".mov" in lower:
            score += 70
        if ".webm" in lower:
            score += 35
        if "video" in content_type:
            score += 50
        if any(token in lower for token in ("/render", "/download", "/video", "/media")):
            score += 20
        if "fetch-response" in str(item.get("source") or "") or "xhr-load" in str(item.get("source") or ""):
            score += 10
        try:
            score += min(int(item.get("transferSize") or item.get("encodedBodySize") or 0) // 50000, 20)
        except Exception:
            pass
        return score

    def _best_captured_heygen_url(self, urls):
        scored = [
            (self._score_heygen_url(item), item)
            for item in urls
        ]
        scored = [pair for pair in scored if pair[0] > 0]
        if not scored:
            return None
        scored.sort(key=lambda pair: (pair[0], pair[1].get("ts") or 0), reverse=True)
        return scored[0][1]

    def _download_captured_heygen_url(self, page, item, task_id, cut_key):
        url = item.get("url")
        response = page.context.request.get(url, timeout=120000)
        if not response.ok:
            raise RuntimeError(f"Captured HeyGen video URL returned HTTP {response.status}.")
        body = response.body()
        if len(body) < 25000:
            raise RuntimeError(f"Captured HeyGen video download was too small ({len(body)} bytes).")
        lower_url = str(url).lower()
        suffix = ".mp4"
        for candidate in (".mp4", ".mov", ".webm", ".m4v"):
            if candidate in lower_url:
                suffix = candidate
                break
        filename = self._safe_output_name(task_id, cut_key, suffix)
        output_path = os.path.join(self.output_dir, filename)
        Path(output_path).write_bytes(body)
        print(f"HEYGEN_CAPTURE_DOWNLOADED: {filename} bytes={len(body)}", flush=True)
        return filename

    def _render_and_download_with_page_capture(self, page, task_id, cut_key, timeout_ms):
        self._install_heygen_capture_hooks(page)
        started = time.time()
        for attempt in range(1, 4):
            self._click_heygen_render_scene(page)
            page.wait_for_timeout(1500)
            self._submit_generate_modal_if_present(page)
            page.wait_for_timeout(7500)
            data = self._captured_heygen_urls(page)
            if self._render_started_signal(page) or data.get("urls"):
                break
            modal_reason = self._heygen_blocking_modal_reason(page)
            if modal_reason:
                print(f"HEYGEN_CAPTURE_BLOCKED_MODAL: {modal_reason}", flush=True)
            print(f"HEYGEN_CAPTURE_RENDER_NOT_STARTED_RETRY: attempt={attempt}", flush=True)
        else:
            reason = self._heygen_blocking_modal_reason(page)
            raise RuntimeError(reason or "HeyGen Render Scene did not start after repeated clicks.")
        deadline = time.time() + (timeout_ms / 1000)
        last_log = 0
        best = None
        while time.time() < deadline:
            data = self._captured_heygen_urls(page)
            best = self._best_captured_heygen_url(data.get("urls") or [])
            if best and (time.time() - started) > 3:
                try:
                    return self._download_captured_heygen_url(page, best, task_id, cut_key)
                except Exception as error:
                    print(f"HEYGEN_CAPTURE_DOWNLOAD_WAITING: {error}", flush=True)
            now = time.time()
            if now - last_log > 12:
                urls = data.get("urls") or []
                preview = (best or {}).get("url") or ""
                print(f"HEYGEN_CAPTURE_POLL: urls={len(urls)} best={preview[:140]}", flush=True)
                last_log = now
            page.wait_for_timeout(3000)
        raise RuntimeError(f"HeyGen backend capture did not finish in time. Best URL: {(best or {}).get('url')}")

    def _render_and_download(self, page, task_id, cut_key, context=None):
        timeout_ms = int(float(self.config.get("render_timeout_seconds") or 900) * 1000)
        try:
            return self._render_and_download_with_page_capture(page, task_id, cut_key, timeout_ms)
        except Exception as error:
            if not (context and self._truthy(self.config.get("allow_streamvault_extension_fallback"))):
                if not self._truthy(self.config.get("allow_native_download_fallback")):
                    raise
                print(f"HEYGEN_CAPTURE_FAILED_FALLING_BACK: {error}", flush=True)
            else:
                print(f"HEYGEN_CAPTURE_FAILED_TRYING_STREAMVAULT: {error}", flush=True)

        if context and self._truthy(self.config.get("allow_streamvault_extension_fallback")):
            try:
                return self._render_and_download_with_streamvault(page, context, task_id, cut_key, timeout_ms)
            except Exception as error:
                if not self._truthy(self.config.get("allow_native_download_fallback")):
                    raise
                print(f"HEYGEN_STREAMVAULT_FAILED_FALLING_BACK: {error}", flush=True)

        click_labels = ["Download All", "Download"]
        deadline = time.time() + (timeout_ms / 1000)
        last_error = None
        while time.time() < deadline:
            for label in click_labels:
                try:
                    button = self._first_visible(page, [label], timeout=2500)
                    if not button:
                        continue
                    with page.expect_download(timeout=25000) as download_info:
                        button.click()
                    download = download_info.value
                    suggested = download.suggested_filename or f"{cut_key}.mp4"
                    suffix = Path(suggested).suffix or ".mp4"
                    filename = self._safe_output_name(task_id, cut_key, suffix)
                    output_path = os.path.join(self.output_dir, filename)
                    download.save_as(output_path)
                    return filename
                except PlaywrightTimeoutError as error:
                    last_error = error
                except Exception as error:
                    last_error = error
            page.wait_for_timeout(5000)
        raise RuntimeError(f"HeyGen render/download did not finish in time: {last_error}")

    def _render_and_download_with_streamvault(self, page, context, task_id, cut_key, timeout_ms):
        worker = self._wait_for_streamvault_worker(context)
        if not worker:
            raise RuntimeError("StreamVault extension is not loaded. Set the unpacked StreamVault extension folder in admin settings.")

        started_ms = int(time.time() * 1000)
        tab_id = worker.evaluate(
            """() => new Promise((resolve) => {
                const timer = setTimeout(() => resolve(null), 5000);
                chrome.tabs.query({ url: '*://app.heygen.com/*' }, (tabs) => {
                    clearTimeout(timer);
                    const tab = tabs.find(t => t.active) || tabs[tabs.length - 1];
                    resolve(tab ? tab.id : null);
                });
            })"""
        )
        if not tab_id:
            raise RuntimeError("StreamVault could not find the active HeyGen editor tab.")

        response = worker.evaluate(
            """(tabId) => new Promise((resolve) => {
                const timer = setTimeout(() => resolve({ resp: null, error: 'Timed out while asking StreamVault to start render.' }), 8000);
                chrome.tabs.sendMessage(tabId, { type: 'START_RENDER' }, (resp) => {
                    clearTimeout(timer);
                    resolve({ resp, error: chrome.runtime.lastError ? chrome.runtime.lastError.message : '' });
                });
            })""",
            tab_id,
        )
        if response.get("error") or not (response.get("resp") or {}).get("ok"):
            raise RuntimeError(response.get("error") or "StreamVault could not start HeyGen render.")
        if (response.get("resp") or {}).get("found") is False:
            raise RuntimeError("StreamVault could not find HeyGen Render Scene button.")
        print(f"HEYGEN_STREAMVAULT_STARTED: tab={tab_id}", flush=True)

        deadline = time.time() + (timeout_ms / 1000)
        last_state = None
        latest_file = None
        requested_media_download = False
        last_log = 0
        while time.time() < deadline:
            try:
                last_state = worker.evaluate(
                    """() => new Promise((resolve) => {
                        chrome.storage.local.get('renderState', (r) => resolve(r.renderState || null));
                    })"""
                )
            except Exception:
                last_state = None

            downloads = self._downloads_search(worker, started_ms)
            complete_downloads = [
                item for item in downloads
                if self._is_streamvault_video_download(item)
            ]
            if complete_downloads:
                latest_file = sorted(complete_downloads, key=lambda item: item.get("startTime") or "")[-1].get("filename")
                break

            if last_state and last_state.get("type") == "RENDER_ERROR":
                raise RuntimeError(last_state.get("error") or "StreamVault reported a render error.")

            if (
                last_state
                and last_state.get("type") == "RENDER_COMPLETE"
                and last_state.get("videoUrl")
                and not requested_media_download
            ):
                download_response = self._streamvault_download_media(
                    worker,
                    {
                        "url": last_state.get("videoUrl"),
                        "filename": f"{task_id}_{cut_key}_streamvault.mp4",
                        "type": "video",
                        "isHLS": False,
                    },
                    tab_id,
                )
                if download_response.get("error") or not (download_response.get("resp") or {}).get("success"):
                    raise RuntimeError(download_response.get("error") or (download_response.get("resp") or {}).get("error") or "StreamVault could not download completed render URL.")
                requested_media_download = True

            media = self._streamvault_media(worker, tab_id)
            now = time.time()
            if now - last_log > 15:
                video_count = len([item for item in media if item.get("type") == "video"])
                state_name = last_state.get("type") if last_state else "none"
                progress = last_state.get("progress") if last_state else ""
                print(f"HEYGEN_STREAMVAULT_POLL: state={state_name} progress={progress} media={len(media)} videos={video_count} requested_download={requested_media_download}", flush=True)
                last_log = now
            video_media = [
                item for item in media
                if item.get("type") == "video"
                and item.get("url")
                and not str(item.get("filename") or "").lower().startswith(("appear_", "disappear_"))
            ]
            if video_media and not requested_media_download:
                newest = sorted(video_media, key=lambda item: item.get("timestamp") or 0)[-1]
                download_response = self._streamvault_download_media(worker, newest, tab_id)
                if download_response.get("error") or not (download_response.get("resp") or {}).get("success"):
                    raise RuntimeError(download_response.get("error") or (download_response.get("resp") or {}).get("error") or "StreamVault could not download captured rendered video.")
                requested_media_download = True

            page.wait_for_timeout(5000)

        if not latest_file:
            raise RuntimeError(f"StreamVault render/download did not finish in time. Last state: {last_state}")

        suffix = Path(latest_file).suffix or ".mp4"
        filename = self._safe_output_name(task_id, cut_key, suffix)
        output_path = os.path.join(self.output_dir, filename)
        shutil.copy2(latest_file, output_path)
        return filename




    def _close_context_safely(self, context, timeout_seconds=20):
        finished = []
        error_holder = []

        def closer():
            try:
                context.close()
                finished.append(True)
            except Exception as error:
                error_holder.append(error)
                finished.append(True)

        thread = threading.Thread(target=closer, daemon=True)
        thread.start()
        thread.join(timeout_seconds)
        if finished:
            return

        profile = str(self.config.get("chrome_user_data_dir") or "").strip()
        if os.name == "nt" and profile:
            escaped = profile.replace("'", "''")
            command = (
                "Get-CimInstance Win32_Process | "
                f"Where-Object {{ $_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*{escaped}*' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            try:
                subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command", command],
                    timeout=20,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                print("HEYGEN_CONTEXT_CLOSE_TIMEOUT_CLEANED: killed orphan Chrome profile processes", flush=True)
            except Exception as error:
                print(f"HEYGEN_CONTEXT_CLOSE_TIMEOUT: {error}", flush=True)
        else:
            print("HEYGEN_CONTEXT_CLOSE_TIMEOUT: continuing without blocking", flush=True)


    def _select_primary_avatar_scene(self, page, avatar_name=""):
        """HeyGen sometimes reopens the draft on a blank extra scene.
        Select the first real scene thumbnail before inserting script.
        """
        try:
            text = page.evaluate("() => (document.body.innerText || '').toLowerCase()")
            if "no avatar" not in text and "add avatar" not in text:
                return
        except Exception:
            return
        try:
            page.wait_for_timeout(1500)
            viewport = page.viewport_size or {"width": 1920, "height": 1080}
            for x, y in (
                (float(viewport["width"]) * 0.31, float(viewport["height"]) * 0.91),
                (595, 985),
                (590, 1005),
                (610, 965),
                (575, 990),
                (600, 944),
            ):
                page.mouse.click(float(x), float(y), click_count=1)
                page.wait_for_timeout(1200)
                text = page.evaluate("() => (document.body.innerText || '').toLowerCase()")
                if "no avatar" not in text and "add avatar" not in text:
                    print("HEYGEN_SCENE_BUILDER: selected first avatar scene", flush=True)
                    return
        except Exception:
            pass
        try:
            clicked_avatar_scene = page.evaluate(
                """() => {
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 12 && rect.height > 12 && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const media = Array.from(document.querySelectorAll('img,canvas,video,[style*="background-image"]'))
                        .filter(visible)
                        .map((el) => ({ el, rect: el.getBoundingClientRect() }))
                        .filter((item) => item.rect.top > window.innerHeight - 170)
                        .filter((item) => item.rect.left > window.innerWidth * 0.25 && item.rect.left < window.innerWidth * 0.78)
                        .filter((item) => item.rect.width >= 24 && item.rect.width <= 130 && item.rect.height >= 35 && item.rect.height <= 150)
                        .sort((a, b) => a.rect.left - b.rect.left);
                    const item = media[0];
                    if (!item) return false;
                    let target = item.el;
                    for (let depth = 0; target && depth < 6; depth += 1) {
                        const rect = target.getBoundingClientRect();
                        if (rect.width >= item.rect.width && rect.height >= item.rect.height && rect.width <= 140 && rect.height <= 170) break;
                        target = target.parentElement;
                    }
                    target = (target && (target.closest('button,[role="button"]') || target)) || item.el;
                    const rect = target.getBoundingClientRect();
                    const x = rect.left + rect.width / 2;
                    const y = rect.top + rect.height / 2;
                    target.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, clientX: x, clientY: y }));
                    target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: x, clientY: y }));
                    target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: x, clientY: y }));
                    target.click();
                    return true;
                }"""
            )
            if clicked_avatar_scene:
                page.wait_for_timeout(1300)
                text = page.evaluate("() => (document.body.innerText || '').toLowerCase()")
                if "no avatar" not in text:
                    return
        except Exception:
            pass
        try:
            point = page.evaluate(
                """() => {
                    const viewportPoint = { x: window.innerWidth * 0.31, y: window.innerHeight * 0.91 };
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 20 && rect.height > 20 && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const cards = Array.from(document.querySelectorAll('img, video, canvas, [style*="background-image"], div'))
                        .filter(visible)
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const text = (el.innerText || el.textContent || '').toLowerCase();
                            return { el, rect, text };
                        })
                        .filter((item) => item.rect.top > window.innerHeight - 180)
                        .filter((item) => item.rect.left > 520 && item.rect.left < 760)
                        .filter((item) => item.rect.width < 160 && item.rect.height < 140)
                        .filter((item) => !item.text.includes('+'))
                        .sort((a, b) => a.rect.left - b.rect.left);
                    const first = cards[0];
                    if (!first) return viewportPoint;
                    return { x: first.rect.left + first.rect.width / 2, y: first.rect.top + first.rect.height / 2 };
                }"""
            )
            for x, y in (
                (float(point.get("x", 595)), float(point.get("y", 995))),
                (page.viewport_size["width"] * 0.31, page.viewport_size["height"] * 0.91),
                (580, 1018),
                (600, 1018),
                (580, 990),
                (610, 990),
                (595, 985),
                (595, 950),
            ):
                page.mouse.click(x, y, click_count=1)
                page.wait_for_timeout(250)
                page.mouse.click(x, y, click_count=1)
                page.wait_for_timeout(900)
                try:
                    page.keyboard.press("Home")
                    page.wait_for_timeout(300)
                    page.keyboard.press("ArrowLeft")
                    page.wait_for_timeout(300)
                except Exception:
                    pass
                try:
                    text = page.evaluate("() => (document.body.innerText || '').toLowerCase()")
                    if "no avatar" not in text:
                        return
                except Exception:
                    return
        except Exception:
            try:
                page.mouse.click(595, 995)
                page.wait_for_timeout(2000)
            except Exception:
                pass
        try:
            page.evaluate("() => (document.body.innerText || '').toLowerCase()")
        except Exception:
            pass
        try:
            deleted_blank = page.evaluate(
                """() => {
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 8 && rect.height > 8 && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const buttons = Array.from(document.querySelectorAll('button,[role="button"],div'))
                        .filter(visible)
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const label = [
                                el.getAttribute('aria-label') || '',
                                el.getAttribute('title') || '',
                                el.innerText || ''
                            ].join(' ').toLowerCase();
                            return { el, rect, label };
                        });
                    const deleteButton = buttons.find((item) =>
                        (item.label.includes('delete') || item.label.includes('remove') || item.label.includes('trash'))
                        && item.rect.left > 420
                        && item.rect.left < 560
                        && item.rect.top > 180
                        && item.rect.top < 280
                    ) || buttons.find((item) =>
                        item.rect.left > 485
                        && item.rect.left < 535
                        && item.rect.top > 210
                        && item.rect.top < 255
                        && item.rect.width <= 50
                        && item.rect.height <= 50
                    );
                    if (!deleteButton) return false;
                    deleteButton.el.click();
                    return true;
                }"""
            )
            if deleted_blank:
                page.wait_for_timeout(1200)
                try:
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(700)
                except Exception:
                    pass
                text = page.evaluate("() => (document.body.innerText || '').toLowerCase()")
                if "no avatar" not in text and "add avatar" not in text:
                    print("HEYGEN_SCENE_BUILDER: removed blank selected scene", flush=True)
                    return
        except Exception:
            pass
        try:
            for x, y in (
                (page.viewport_size["width"] * 0.267, page.viewport_size["height"] * 0.217),
                (513, 234),
                (510, 235),
            ):
                page.mouse.click(float(x), float(y))
                page.wait_for_timeout(900)
                try:
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(700)
                except Exception:
                    pass
                text = page.evaluate("() => (document.body.innerText || '').toLowerCase()")
                if "no avatar" not in text and "add avatar" not in text:
                    print("HEYGEN_SCENE_BUILDER: removed blank selected scene via coordinate fallback", flush=True)
                    return
        except Exception:
            pass
        self._add_avatar_to_current_scene(page, avatar_name)

    def _add_avatar_to_current_scene(self, page, avatar_name):
        group_name, look_name, requested_label = self._avatar_selector_parts(avatar_name)
        labels = [item for item in (look_name, requested_label, group_name) if item]
        try:
            if not self._click_first_force(page, ["No Avatar", "Add avatar", "Add Avatar"], timeout=3000):
                page.evaluate(
                    """() => {
                        const visible = (el) => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 20 && rect.height > 20 && style.display !== 'none' && style.visibility !== 'hidden';
                        };
                        const node = Array.from(document.querySelectorAll('button,[role="button"],div'))
                            .filter(visible)
                            .find((el) => /no avatar|add avatar/i.test(el.innerText || el.textContent || ''));
                        if (node) node.click();
                    }"""
                )
            page.wait_for_timeout(1500)
            try:
                body_text = page.evaluate("() => (document.body.innerText || '').toLowerCase()")
                if "choose avatar" not in body_text and "my avatars" not in body_text:
                    page.mouse.click(1645, 170)
                    page.wait_for_timeout(1800)
            except Exception:
                pass
            selected = page.evaluate(
                """(labels) => {
                    const wanted = labels.map((label) => String(label || '').trim().toLowerCase()).filter(Boolean);
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 30 && rect.height > 30 && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const nodes = Array.from(document.querySelectorAll('button,[role="button"],div'))
                        .filter(visible)
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                            return { el, rect, text };
                        })
                        .filter((item) => item.rect.left > window.innerWidth * 0.48)
                        .filter((item) => item.rect.width < 760 && item.rect.height < 420)
                        .filter((item) => item.text.length < 500)
                        .filter((item) => !item.text.includes('all-in-one ai video generator'))
                        .filter((item) => wanted.some((label) => item.text.includes(label)))
                        .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));
                    const item = nodes[0];
                    if (!item) return { ok: false };
                    const target = item.el.closest('button,[role="button"]') || item.el;
                    target.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
                    target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                    target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                    target.click();
                    return { ok: true, text: item.text.slice(0, 120) };
                }""",
                labels,
            )
            if selected and selected.get("ok"):
                print(f"HEYGEN_SCENE_BUILDER: added avatar to blank scene via {selected.get('text')!r}", flush=True)
                page.wait_for_timeout(2500)
        except Exception as exc:
            print(f"HEYGEN_SCENE_BUILDER: could not repair blank scene avatar: {type(exc).__name__}: {exc}", flush=True)

    def _paste_text_via_clipboard(self, page, text):
        try:
            page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://app.heygen.com")
        except Exception:
            pass
        try:
            page.evaluate("text => navigator.clipboard.writeText(text)", text)
            page.keyboard.press("Control+V")
            return True
        except Exception:
            try:
                page.keyboard.insert_text(text)
                return True
            except Exception:
                return False

    def _normalized_script_text(self, value):
        return " ".join(str(value or "").split()).strip()

    def _script_editor_text(self, page):
        try:
            return str(page.evaluate(
                """() => {
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 20 && rect.height > 10 && rect.left < 650 && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const candidates = Array.from(document.querySelectorAll('textarea,[contenteditable="true"],[data-slate-editor="true"],[role="textbox"],div.ProseMirror'))
                        .filter(visible)
                        .map((el) => ({
                            text: ('value' in el ? el.value : (el.innerText || el.textContent || '')).replace(/\\s+/g, ' ').trim(),
                            area: el.getBoundingClientRect().width * el.getBoundingClientRect().height
                        }))
                        .filter((item) => item.text);
                    candidates.sort((a, b) => b.text.length - a.text.length || b.area - a.area);
                    return candidates[0]?.text || '';
                }"""
            ) or "")
        except Exception:
            return ""

    def _script_editor_state(self, page, script_text):
        expected = self._normalized_script_text(script_text).lower()
        current = self._normalized_script_text(self._script_editor_text(page)).lower()
        marker = expected[:80]
        if not expected:
            return {"present": False, "exact": False, "duplicate": False, "current": ""}
        occurrences = current.count(marker) if marker and current else 0
        return {
            "present": bool(marker and marker in current),
            "exact": current == expected,
            "duplicate": occurrences > 1 or (bool(marker and marker in current) and len(current) > max(len(expected) + 80, int(len(expected) * 1.35))),
            "current": current,
        }

    def _clear_script_editor(self, page):
        try:
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(250)
        except Exception:
            pass
        try:
            page.evaluate(
                """() => {
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 20 && rect.height > 10 && rect.left < 650 && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    for (const el of Array.from(document.querySelectorAll('textarea,[contenteditable="true"],[data-slate-editor="true"],[role="textbox"],div.ProseMirror')).filter(visible)) {
                        el.focus();
                        if ('value' in el) {
                            el.value = '';
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        } else {
                            el.textContent = '';
                            el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward' }));
                        }
                    }
                }"""
            )
            page.wait_for_timeout(250)
        except Exception:
            pass

    def _heygen_script_voice_ready(self, page):
        try:
            return bool(page.evaluate(
                """() => {
                    const text = (document.body.innerText || '').toLowerCase();
                    if (/00:00\s*\/\s*00:0[1-9]/.test(text) || /00:00\s*\/\s*00:[1-5][0-9]/.test(text)) return true;
                    const buttons = Array.from(document.querySelectorAll('button,[role="button"]'));
                    return buttons.some((button) => {
                        const label = [button.innerText || '', button.getAttribute('aria-label') || '', button.getAttribute('title') || ''].join(' ').toLowerCase();
                        const disabled = button.disabled || button.getAttribute('aria-disabled') === 'true';
                        return /render\s+scene|generate/.test(label) && !disabled;
                    });
                }"""
            ))
        except Exception:
            return False

    def _commit_script_and_wait_for_voice(self, page):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(700)
            page.mouse.click(1000, 120)
            page.wait_for_timeout(1000)
        except Exception:
            pass
        deadline = time.time() + int(float(self.config.get("script_voice_timeout_seconds") or 90))
        last_try = 0
        while time.time() < deadline:
            if self._heygen_script_voice_ready(page):
                return True
            now = time.time()
            if now - last_try > 12:
                last_try = now
                try:
                    page.keyboard.press("Escape")
                    page.mouse.click(1000, 120)
                    page.wait_for_timeout(800)
                    play_buttons = page.locator("button,[role='button']")
                    count = play_buttons.count()
                    for index in range(min(count, 20)):
                        label = ""
                        try:
                            label = " ".join([
                                play_buttons.nth(index).inner_text(timeout=300) or "",
                                play_buttons.nth(index).get_attribute("aria-label", timeout=300) or "",
                                play_buttons.nth(index).get_attribute("title", timeout=300) or "",
                            ]).lower()
                        except Exception:
                            continue
                        if "play" in label or "preview" in label:
                            play_buttons.nth(index).click(timeout=800, force=True)
                            break
                except Exception:
                    pass
            page.wait_for_timeout(2000)
        raise RuntimeError("HeyGen script was pasted, but voice generation did not become ready.")

    def _set_script_text(self, page, script_text):
        script_text = str(script_text or "").strip()
        if not script_text:
            raise RuntimeError("HeyGen script text is empty.")
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            pass

        state = self._script_editor_state(page, script_text)
        if state.get("exact") and not state.get("duplicate"):
            print("HEYGEN_SCRIPT: script already present; skipping paste", flush=True)
            return
        if state.get("duplicate"):
            print("HEYGEN_SCRIPT: duplicate script detected; clearing editor before paste", flush=True)
            self._clear_script_editor(page)

        selectors = [
            "textarea",
            "[contenteditable='true']",
            "[role='textbox']",
            "div.ProseMirror",
            "div[aria-label*='script' i]",
        ]
        for selector in selectors:
            loc = page.locator(selector)
            try:
                count = loc.count()
            except Exception:
                count = 0
            for index in range(count):
                field = loc.nth(index)
                try:
                    box = field.bounding_box(timeout=1500)
                    if not box or box.get("width", 0) < 50 or box.get("height", 0) < 20:
                        continue
                    field.click(timeout=3000, force=True)
                    self._clear_script_editor(page)
                    field.fill(script_text, timeout=5000)
                    page.wait_for_timeout(800)
                    state = self._script_editor_state(page, script_text)
                    if state.get("exact") or (self._script_text_present(page, script_text) and not state.get("duplicate")):
                        return
                except Exception:
                    try:
                        field.click(timeout=3000, force=True)
                        self._clear_script_editor(page)
                        self._paste_text_via_clipboard(page, script_text)
                        page.wait_for_timeout(800)
                        state = self._script_editor_state(page, script_text)
                        if state.get("exact") or (self._script_text_present(page, script_text) and not state.get("duplicate")):
                            return
                    except Exception:
                        continue

        clicked = page.evaluate(
            """() => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 50 && rect.height > 20 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const candidates = Array.from(document.querySelectorAll("textarea,[contenteditable='true'],[role='textbox'],div.ProseMirror,div"))
                    .filter(visible)
                    .filter((el) => /script|type your script|commands|upload audio/i.test([
                        el.innerText || '', el.textContent || '', el.getAttribute('placeholder') || '', el.getAttribute('aria-label') || ''
                    ].join(' ')));
                const node = candidates[0] || Array.from(document.querySelectorAll("textarea,[contenteditable='true'],[role='textbox'],div.ProseMirror")).filter(visible)[0];
                if (!node) return false;
                node.click();
                return true;
            }"""
        )
        if clicked:
            self._clear_script_editor(page)
            self._paste_text_via_clipboard(page, script_text)
            page.wait_for_timeout(1000)
            state = self._script_editor_state(page, script_text)
            if state.get("exact") or (self._script_text_present(page, script_text) and not state.get("duplicate")):
                return

        # HeyGen's current script editor can be a custom block. Use exact
        # coordinates from the visible editor area, then try paste and typing.
        for x, y in ((135, 155), (110, 195), (145, 195), (185, 195)):
            for click_count in (1, 2):
                try:
                    page.mouse.click(x, y, click_count=click_count)
                    page.wait_for_timeout(500)
                    self._clear_script_editor(page)
                    self._paste_text_via_clipboard(page, script_text)
                    page.wait_for_timeout(1200)
                    state = self._script_editor_state(page, script_text)
                    if state.get("exact") or (self._script_text_present(page, script_text) and not state.get("duplicate")):
                        return
                    self._clear_script_editor(page)
                    page.mouse.click(x, y, click_count=click_count)
                    page.wait_for_timeout(300)
                    page.keyboard.type(script_text, delay=2)
                    page.wait_for_timeout(1200)
                    state = self._script_editor_state(page, script_text)
                    if state.get("exact") or (self._script_text_present(page, script_text) and not state.get("duplicate")):
                        return
                except Exception:
                    pass
            try:
                page.mouse.click(x, y, click_count=1)
                page.wait_for_timeout(500)
            except Exception:
                pass

        try:
            inserted = page.evaluate(
                """(scriptText) => {
                    const candidates = Array.from(document.querySelectorAll('[contenteditable="true"], [data-slate-editor="true"], [role="textbox"], textarea'))
                        .filter((el) => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 10 && rect.height > 10 && rect.left < 650 && rect.top < 260 && style.display !== 'none' && style.visibility !== 'hidden';
                        });
                    const el = candidates[0];
                    if (!el) return false;
                    el.focus();
                    if ('value' in el) {
                        el.value = scriptText;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                    el.textContent = scriptText;
                    el.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, inputType: 'insertText', data: scriptText }));
                    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: scriptText }));
                    return true;
                }""",
                script_text,
            )
            page.wait_for_timeout(1200)
            if inserted and self._script_text_present(page, script_text):
                return
        except Exception:
            pass
        raise RuntimeError("Could not paste script into HeyGen editor.")

    def _script_text_present(self, page, script_text):
        marker = " ".join(str(script_text or "").split())[:80].lower()
        if not marker:
            return False
        try:
            body = page.evaluate("() => (document.body.innerText || document.body.textContent || '').replace(/\\s+/g, ' ').toLowerCase()")
            return marker in body
        except Exception:
            return False

    def render_script(self, script_text, cut_key, label, talent_key, avatar_name, task_id, on_status=None):
        if sync_playwright is None:
            raise RuntimeError("Playwright is not installed. Run setup again or install backend requirements.")
        script_text = str(script_text or "").strip()
        if not script_text:
            raise RuntimeError(f"Script text does not exist for {label}.")

        if on_status:
            on_status(f"Opening HeyGen for {label}...", None)

        with sync_playwright() as playwright:
            context = self._launch_context(playwright)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                self._open_scene_builder(page, avatar_name)
                self._select_primary_avatar_scene(page, avatar_name)
                if on_status:
                    on_status(f"Pasting {label} script into HeyGen...", None)
                self._set_script_text(page, script_text)
                self._commit_script_and_wait_for_voice(page)
                self._select_motion_engine(page)
                if on_status:
                    on_status(f"Rendering {label} in HeyGen...", None)
                return self._render_and_download(page, task_id, cut_key, context=context)
            except Exception as error:
                debug_path = self._debug_screenshot(page, task_id, cut_key)
                suffix = f" Debug screenshot: {debug_path}" if debug_path else ""
                raise RuntimeError(f"{error}{suffix}") from error
            finally:
                self._close_context_safely(context)

    def render_cut(self, audio_path, cut_key, label, talent_key, avatar_name, task_id, on_status=None):
        if sync_playwright is None:
            raise RuntimeError("Playwright is not installed. Run setup again or install backend requirements.")
        audio_path = str(audio_path or "").strip()
        if not audio_path or not Path(audio_path).exists():
            raise RuntimeError(f"Audio file does not exist for {label}.")

        if on_status:
            on_status(f"Opening HeyGen for {label}...", None)

        with sync_playwright() as playwright:
            context = self._launch_context(playwright)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                self._open_scene_builder(page, avatar_name)
                self._select_primary_avatar_scene(page, avatar_name)
                if on_status:
                    on_status(f"Uploading {label} audio to HeyGen...", None)
                self._upload_audio(page, audio_path)
                self._select_motion_engine(page)
                if on_status:
                    on_status(f"Rendering {label} in HeyGen...", None)
                return self._render_and_download(page, task_id, cut_key, context=context)
            except Exception as error:
                debug_path = self._debug_screenshot(page, task_id, cut_key)
                suffix = f" Debug screenshot: {debug_path}" if debug_path else ""
                raise RuntimeError(f"{error}{suffix}") from error
            finally:
                self._close_context_safely(context)



