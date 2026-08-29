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

AUTH_KEY = os.environ.get("AUTH_KEY", "th-web-key")
PORT = int(os.environ.get("PORT", "8000"))
MODEL = os.environ.get("MODEL", "alibaba/deepseek-v4-flash:free")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"

# ============ 账号解析 ============
def parse_accounts():
    raw = os.environ.get("TH_ACCOUNTS", "")
    accts = []
    if raw:
        for part in raw.split(";"):
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
        # 额度用尽则冷却 60 分钟 (Token Harbor 滚动周期较久, 保守处理)
        if self.exhausted:
            if time.time() - self.exhausted_at > 3600:
                self.exhausted = False
            else:
                return False
        return True

    def mark_exhausted(self):
        self.exhausted = True
        self.exhausted_at = time.time()
        print(f"[TH] 账号 {self.email[:20]} 额度用尽, 已标记")

    def refresh(self):
        """用 undetected-chromedriver 登录并提取 session"""
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
        driver = uc.Chrome(options=opts, version_main=int(chrome_ver) if chrome_ver else None)

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


# ============ 多账号管理器 ============
class THAccountPool:
    def __init__(self, accounts):
        self.sessions = [THSession(e, p) for e, p in accounts]
        self.pool_lock = threading.Lock()
        self.cursor = 0

    def all_sessions(self):
        return self.sessions

    def init_all(self):
        """启动时并发登录所有账号"""
        def do_login(s):
            try:
                s.refresh()
            except Exception as e:
                print(f"[TH] 账号 {s.email[:20]} 登录失败: {e}")
        threads = [threading.Thread(target=do_login, args=(s,), daemon=True) for s in self.sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        ok = [s for s in self.sessions if s.is_valid()]
        print(f"[TH] 初始登录完成: {len(ok)}/{len(self.sessions)} 账号就绪")

    def pick(self):
        """round-robin 挑一个可用账号"""
        with self.pool_lock:
            n = len(self.sessions)
            for i in range(n):
                idx = (self.cursor + i) % n
                s = self.sessions[idx]
                if s.can_use() and (s.is_valid() or s.error_count < 3):
                    self.cursor = (idx + 1) % n
                    return s
            # 全部不可用, 强制用第一个
            return self.sessions[0]

    def refresh_all(self):
        for s in self.sessions:
            if not s.is_valid():
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
            self._send_json(200, {"status": "ok", "accounts_ready": len([s for s in pool.all_sessions() if s.is_valid()])})
            return
        if not self._auth_ok():
            self._send_json(401, {"error": {"message": "Unauthorized"}})
            return
        if self.path.startswith("/v1/models"):
            self._send_json(200, {"object": "list", "data": [
                {"id": "deepseek-v4-flash:free", "object": "model", "owned_by": "tokenharbor"},
            ]})
        else:
            self._send_json(404, {"error": {"message": "Not Found"}})

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

        # 多账号接力: 依次尝试, 额度尽切下一个
        tried = set()
        full_text = ""
        success = False

        for attempt in range(len(pool.all_sessions())):
            s = pool.pick()
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
