#!/usr/bin/env python3
"""
Token Harbor Web Session Proxy (多账号接力版)
用真实浏览器登录多个 tokenharbor 账号, 提取 web session, 转发到内部端点。
一个账号额度用尽自动切换下一个。

环境变量:
  TH_ACCOUNTS  账号列表, 分号分隔: email1:pass1;email2:pass2
               兼容旧的 TH_EMAIL/TH_PASS (单个账号)
  AUTH_KEY     客户端鉴权 key (请求头 Authorization: Bearer <AUTH_KEY>)
  PORT         监听端口 (默认 8000)
  MODEL        默认模型 (默认 alibaba/deepseek-v4-flash:free)
"""
import json, os, re, time, uuid, threading, urllib.request, urllib.error
import http.server, socketserver

LOGIN_LOCK = threading.Lock()  # 全局登录锁: 一次只登录一个浏览器

AUTH_KEY = os.environ.get("AUTH_KEY", "th-web-key")
PORT = int(os.environ.get("PORT", "8000"))
MODEL = os.environ.get("MODEL", "alibaba/deepseek-v4-flash:free")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"

# ============ 账号解析 ============
def parse_accounts():
    raw = os.environ.get("TH_ACCOUNTS", "")
    accts = []
    if raw:
        # 兼容分号或换行分隔 (accounts.txt 一行一个)
        for part in re.split(r"[;\n]", raw):
            part = part.strip()
            if ":" in part:
                e, p = part.split(":", 1)
                accts.append((e.strip(), p.strip()))
    # 兼容旧版单账号
    e2 = os.environ.get("TH_EMAIL", "")
    p2 = os.environ.get("TH_PASS", "")
    if e2 and p2 and (e2, p2) not in accts:
        accts.append((e2, p2))
    return accts


ACCOUNTS = parse_accounts()

# ============ 单账号 Session ============
class THSession:
    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.cookie = ""
        self.session_id = ""
        self.lock = threading.Lock()
        self.last_refresh = 0
        self.exhausted = False      # 额度用尽标记
        self.exhausted_at = 0       # 用尽时间 (用于重置后恢复)
        self.error_count = 0

    def is_valid(self):
        return bool(self.cookie) and (time.time() - self.last_refresh) < 1800

    def can_use(self):
        # 额度用尽则冷却 7 天 (Token Harbor 7天滚动周期, 用完等重置)
        if self.exhausted:
            if time.time() - self.exhausted_at > 7*24*3600:
                self.exhausted = False
            else:
                return False
        return True

    def mark_exhausted(self):
        self.exhausted = True
        self.exhausted_at = time.time()
        print(f"[TH] 账号 {self.email[:20]} 额度用尽, 已标记")

    def refresh(self):
        """用 undetected-chromedriver 登录并提取 session (全局锁, 串行)"""
        with LOGIN_LOCK:
            return self._refresh_locked()

    def _refresh_locked(self):
        import undetected_chromedriver as uc
        from selenium.webdriver.common.by import By

        opts = uc.ChromeOptions()
        opts.add_argument("--no-sandbox")
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--user-data-dir=/tmp/th_chrome")
        import subprocess
        chrome_ver = ""
        try:
            out = subprocess.run(
                ["google-chrome", "--version"], capture_output=True, text=True, timeout=10)
            m = re.search(r"(\d+)\.", out.stdout or "")
            if m:
                chrome_ver = m.group(1)
        except Exception:
            pass
        # 使用预装的 chromedriver, 避免 uc 运行时下载冲突
        driver = uc.Chrome(
            options=opts,
            version_main=int(chrome_ver) if chrome_ver else None,
            driver_executable_path="/usr/local/bin/chromedriver",
        )

        try:
            driver.get("https://tokenharbor.ai/login")
            time.sleep(12)
            email_box = driver.find_element(By.CSS_SELECTOR, "input[type=email]")
            email_box.clear()
            email_box.send_keys(self.email)
            pw = driver.find_element(By.CSS_SELECTOR, "input[type=password]")
            pw.clear()
            pw.send_keys(self.password)
            time.sleep(2)
            for txt in ["Sign in", "Login", "Continue"]:
                try:
                    btns = driver.find_elements(By.XPATH, f"//button[contains(., '{txt}')]")
                    if btns:
                        btns[-1].click()
                        break
                except Exception:
                    pass
            time.sleep(15)

            if "/login" in driver.current_url:
                raise RuntimeError(f"登录失败: {self.email}")

            driver.get("https://tokenharbor.ai/chat")
            time.sleep(10)
            cur = driver.current_url
            m = re.search(r"/chat/([a-f0-9-]{32,})", cur)
            sid = m.group(1) if m else str(uuid.uuid4())
            cookie = driver.execute_script("return document.cookie")

            with self.lock:
                self.cookie = cookie
                self.session_id = sid
                self.last_refresh = time.time()
                self.error_count = 0
            print(f"[TH] 账号 {self.email[:25]} session 就绪: sid={sid[:12]}... cookie_len={len(cookie)}")
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    def fetch_dashboard(self):
        """用当前 cookie 请求 dashboard 页面, 返回 HTML"""
        req = urllib.request.Request(
            "https://tokenharbor.ai/dashboard",
            headers={
                "Cookie": self.cookie,
                "User-Agent": UA,
            })
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")

    def chat_stream(self, content, model, callback):
        """调内部端点, SSE 流式, 返回 True=成功 / False=额度限制"""
        body = json.dumps({
            "sessionId": self.session_id,
            "content": content,
            "model": model,
            "webSearch": "auto"}).encode()
        req = urllib.request.Request(
            "https://tokenharbor.ai/api/direct-chat/stream",
            data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Cookie": self.cookie,
                "Origin": "https://tokenharbor.ai",
                "Referer": f"https://tokenharbor.ai/chat/{self.session_id}",
                "User-Agent": UA,
            })
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                for line in r:
                    line = line.decode("utf-8", "replace").rstrip("\n")
                    if line:
                        callback(line)
                return True
        except urllib.error.HTTPError as e:
            body_err = e.read().decode("utf-8", "replace")
            # 429 = 额度用尽; 401 = session 失效
            if e.code == 429 or "quota" in body_err or "allowance" in body_err or "limit" in body_err:
                self.mark_exhausted()
                return False
            if e.code == 401:
                self.error_count += 1
                return False
            print(f"[TH] {self.email[:20]} 上游错误 {e.code}: {body_err[:150]}")
            self.error_count += 1
            return False
        except Exception:
            self.error_count += 1
            return False


# ============ 多账号管理器 (懒加载) ============
class THAccountPool:
    def __init__(self, accounts, initial=1):
        self.all_accounts = [(e, p) for e, p in accounts]   # 全部账号 (待加载池)
        self.sessions = []                                   # 已加载的 session
        self.pool_lock = threading.Lock()
        self.cursor = 0
        self.initial = initial                              # 启动加载数
        self.failed_accounts = {}                           # email -> 失败次数

    def _load(self, email, password):
        """加载一个账号"""
        s = THSession(email, password)
        try:
            s.refresh()
        except Exception as e:
            print(f"[TH] 账号 {email[:25]} 登录失败: {e}")
            return None
        if not s.is_valid():
            return None
        with self.pool_lock:
            self.sessions.append(s)
        print(f"[TH] 已加载账号 {email[:25]}, 当前在线: {len(self.sessions)}")
        return s

    def init_all(self):
        """启动时加载前 N 个账号"""
        print(f"[TH] 启动加载 {min(self.initial, len(self.all_accounts))} 个账号 (共 {len(self.all_accounts)} 个)...")
        for _ in range(min(self.initial, len(self.all_accounts))):
            if not self.all_accounts:
                break
            email, password = self.all_accounts.pop(0)
            self._load(email, password)
        ok = len(self.sessions)
        print(f"[TH] 启动完成: {ok} 个账号就绪, 待加载 {len(self.all_accounts)} 个")

    def pick(self):
        """round-robin 挑一个可用账号; 不够则懒加载新的"""
        with self.pool_lock:
            n = len(self.sessions)
            if n == 0:
                # 全部加载失败, 从待加载池补
                if self.all_accounts:
                    email, password = self.all_accounts.pop(0)
                    s = self._load(email, password)
                    if s:
                        return s
                return None
            for i in range(n):
                idx = (self.cursor + i) % n
                s = self.sessions[idx]
                if s.can_use() and (s.is_valid() or s.error_count < 3):
                    self.cursor = (idx + 1) % n
                    return s
            # 全部暂时不可用: 尝试懒加载一个新账号
            if self.all_accounts:
                email, password = self.all_accounts.pop(0)
                s = self._load(email, password)
                if s:
                    return s
            # 没有新账号了, 强制用第一个
            return self.sessions[0] if self.sessions else None

    def mark_used(self, s):
        """账号被用尽后, 尝试换新账号"""
        pass

    def refresh_all(self):
        for s in self.sessions:
            if not s.is_valid() and s.error_count < 3:
                try:
                    s.refresh()
                except Exception as e:
                    print(f"[TH] 刷新 {s.email[:20]} 失败: {e}")


pool = THAccountPool(ACCOUNTS)

# ============ OpenAI 兼容 HTTP 服务 ============
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _auth_ok(self):
        h = self.headers.get("Authorization", "")
        return h == f"Bearer {AUTH_KEY}"

    def _send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/healthz", "/health"):
            self._send_json(200, {"status": "ok", "accounts_ready": len([s for s in pool.sessions if s.is_valid()]), "pending": len(pool.all_accounts)})
            return
        if not self._auth_ok():
            self._send_json(401, {"error": {"message": "Unauthorized"}})
            return
        if self.path.startswith("/v1/models"):
            self._send_json(200, {"object": "list", "data": [
                {"id": "deepseek-v4-flash:free", "object": "model", "owned_by": "tokenharbor"},
            ]})
        elif self.path.startswith("/v1/usage"):
            self._handle_usage(debug="debug" in self.path)
        else:
            self._send_json(404, {"error": {"message": "Not Found"}})

    def _handle_usage(self, debug=False):
        """查看当前账号的免费额度 (从 dashboard HTML 解析)"""
        with pool.pool_lock:
            s = pool.sessions[0] if pool.sessions else None
        if not s or not s.is_valid():
            self._send_json(503, {"error": {"message": "No active session"}})
            return
        try:
            html = s.fetch_dashboard()

            allowance = ""
            resets = ""
            # 数据可能在 Next.js RSC payload (self.__next_f.push) 里
            # 提取所有 RSC 内容拼成一个大字符串
            rsc_parts = re.findall(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)', html)
            rsc_text = "".join(
                part.encode().decode("unicode_escape", "replace") if "\\u" in part else part
                for part in rsc_parts
            )
            search_scope = html + "\n" + rsc_text

            # 定位 Free 相关区块 (精确 anchor, 避免 meta description 误匹配)
            free_block = ""
            for anchor in ["Free allowance", "free allowance", "allowance", "Resets in", "resets in"]:
                idx = search_scope.find(anchor)
                if idx >= 0:
                    free_block = search_scope[max(0, idx-300):idx+1500]
                    break
            if not free_block:
                free_block = search_scope

            # 找百分比 "0% used" 等
            m = re.search(r'(\d+(?:\.\d+)?)\s*%\s*used', free_block, re.IGNORECASE)
            if m:
                allowance = m.group(1) + "% used"
            # 找重置时间 "Resets in 6d 16h" / "resetsIn":"6d 16h" 等
            m2 = re.search(r'Resets?\s*in\s*([\d.]+d\s*[\d.]+h)', free_block, re.IGNORECASE)
            if not m2:
                m2 = re.search(r'[Rr]esets?[Ii]n?[^0-9]{0,40}?([\d.]+d\s*[\d.]+h)', free_block)
            if m2:
                resets = m2.group(1)

            self._send_json(200, {
                "account": s.email,
                "free_allowance_used": allowance or "unknown",
                "resets_in": resets or "unknown",
                "accounts_ready": len([x for x in pool.sessions if x.is_valid()]),
                "pending": len(pool.all_accounts),
                **({"debug_block": free_block[:600] if free_block else "NO BLOCK FOUND", "rsc_len": len(rsc_text)} if debug else {})
            })
        except Exception as e:
            self._send_json(502, {"error": {"message": f"Failed to fetch usage: {e}"}})

    def do_POST(self):
        if not self._auth_ok():
            self._send_json(401, {"error": {"message": "Unauthorized"}})
            return
        if not self.path.startswith("/v1/chat/completions"):
            self._send_json(404, {"error": {"message": "Not Found"}})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length))
        except Exception as e:
            self._send_json(400, {"error": {"message": f"Bad request: {e}"}})
            return

        messages = req.get("messages", [])
        content = messages[-1].get("content", "") if messages else ""
        stream = req.get("stream", False)
        client_model = req.get("model", "")
        model = MODEL
        if client_model:
            if client_model.startswith("alibaba/"):
                model = client_model
            elif "deepseek" in client_model:
                model = "alibaba/deepseek-v4-flash:free"
            else:
                model = MODEL

        # 多账号接力: 依次尝试, 额度尽切下一个 (懒加载)
        tried = set()
        full_text = ""
        success = False

        for attempt in range(5):  # 最多尝试 5 次
            s = pool.pick()
            if s is None:
                break
            if id(s) in tried:
                break
            tried.add(id(s))

            if not s.is_valid():
                try:
                    s.refresh()
                except Exception:
                    continue
            if not s.can_use():
                continue

            print(f"[TH] 使用账号: {s.email[:25]} (尝试 {attempt+1})")

            def on_line(line, _acc=None):
                nonlocal full_text
                full_text += _parse_sse(line)

            ok = s.chat_stream(content, model, on_line)
            if ok:
                success = True
                break
            # 失败继续下一个账号

        if not success:
            self._send_json(503, {"error": {"message": "All accounts unavailable (quota exhausted or login failed)"}})
            return

        if not full_text:
            full_text = "(empty response)"

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            chunk = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:20]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": client_model or "deepseek-v4-flash:free",
                "choices": [{"index": 0, "delta": {"content": full_text}, "finish_reason": None}],
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            done = {
                "id": chunk["id"], "object": "chat.completion.chunk",
                "created": chunk["created"], "model": chunk["model"],
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            self.wfile.write(f"data: {json.dumps(done)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self._send_json(200, {
                "id": f"chatcmpl-{uuid.uuid4().hex[:20]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": client_model or "deepseek-v4-flash:free",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": full_text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })


def _parse_sse(line):
    """从内部端点 SSE 行提取文本"""
    if line.startswith("data: "):
        try:
            d = json.loads(line[6:])
            for key in ("token", "content", "text", "delta", "message", "chunk"):
                if key in d and isinstance(d[key], str):
                    return d[key]
                if key in d and isinstance(d[key], dict):
                    inner = d[key]
                    for k2 in ("content", "text", "token"):
                        if k2 in inner and isinstance(inner[k2], str):
                            return inner[k2]
                    break
        except Exception:
            pass
    return ""


def main():
    if not ACCOUNTS:
        print("[TH] ❌ 未配置账号! 设置 TH_ACCOUNTS='email1:pass1;email2:pass2'")
        raise SystemExit(1)
    print(f"[TH] Token Harbor web proxy 启动: port={PORT} model={MODEL}")
    print(f"[TH] 账号数: {len(ACCOUNTS)}")
    for e, _ in ACCOUNTS:
        print(f"  - {e}")
    pool.init_all()

    # 后台定时刷新所有 session (每 25 分钟)
    def bg_refresh():
        while True:
            time.sleep(1500)
            try:
                pool.refresh_all()
            except Exception as e:
                print(f"[TH] 后台刷新失败: {e}")

    threading.Thread(target=bg_refresh, daemon=True).start()

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"[TH] 监听 0.0.0.0:{PORT}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
