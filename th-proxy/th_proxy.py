#!/usr/bin/env python3
"""
Token Harbor Web Session Proxy
用真实浏览器登录 tokenharbor, 提取 web session, 转发到内部端点 /api/direct-chat/stream。
不消耗 API key 额度, 一个账号即可。

用法:
  TH_EMAIL=xxx TH_PASS=xxx AUTH_KEY=xxx python3 th_proxy.py

环境变量:
  TH_EMAIL    tokenharbor 账号
  TH_PASS     密码
  AUTH_KEY    客户端鉴权 key (请求头 Authorization: Bearer <AUTH_KEY>)
  PORT        监听端口 (默认 8000)
  MODEL       默认模型 (默认 alibaba/deepseek-v4-flash:free)
"""
import json, os, re, time, uuid, threading, queue, urllib.request, urllib.error
import http.server, socketserver, base64

EMAIL = os.environ.get("TH_EMAIL", "")
PASS = os.environ.get("TH_PASS", "")
AUTH_KEY = os.environ.get("AUTH_KEY", "th-web-key")
PORT = int(os.environ.get("PORT", "8000"))
MODEL = os.environ.get("MODEL", "alibaba/deepseek-v4-flash:free")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"

# ============ Session 管理 ============
class THSession:
    def __init__(self):
        self.cookie = ""
        self.session_id = ""
        self.lock = threading.Lock()
        self.last_refresh = 0

    def is_valid(self):
        return bool(self.cookie) and (time.time() - self.last_refresh) < 1800

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
        # 自动检测 chrome 版本, 让 uc 下载匹配的 chromedriver
        import subprocess
        chrome_ver = ""
        try:
            out = subprocess.run(
                ["google-chrome", "--version"], capture_output=True, text=True, timeout=10)
            chrome_ver = re.search(r"(\d+)\.", out.stdout or "").group(1)
            print(f"[TH] 检测到 Chrome 主版本: {chrome_ver}")
        except Exception:
            pass
        driver = uc.Chrome(options=opts, version_main=int(chrome_ver) if chrome_ver else None)

        try:
            # 登录
            driver.get("https://tokenharbor.ai/login")
            time.sleep(12)
            email_box = driver.find_element(By.CSS_SELECTOR, "input[type=email]")
            email_box.clear()
            email_box.send_keys(EMAIL)
            pw = driver.find_element(By.CSS_SELECTOR, "input[type=password]")
            pw.clear()
            pw.send_keys(PASS)
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
                raise RuntimeError("登录失败, 仍在登录页")

            # 访问 /chat 获取 sessionId
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
            print(f"[TH] session 刷新成功: sid={sid[:20]}... cookie_len={len(cookie)}")
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    def chat_stream(self, content, model, callback):
        """调内部端点, SSE 流式, callback(line)"""
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
        with urllib.request.urlopen(req, timeout=300) as r:
            for line in r:
                line = line.decode("utf-8", "replace").rstrip("\n")
                if line:
                    callback(line)


session = THSession()

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
        if not self._auth_ok():
            self._send_json(401, {"error": {"message": "Unauthorized"}})
            return
        # 健康检查: Northflank 需要 / 或 /healthz 返回 200
        if self.path in ("/", "/healthz", "/health"):
            self._send_json(200, {"status": "ok"})
        elif self.path.startswith("/v1/models"):
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
        model = MODEL
        # 支持客户端传模型, 自动转 alibaba/ 前缀
        client_model = req.get("model", "")
        if client_model:
            if client_model.startswith("alibaba/"):
                model = client_model
            elif "deepseek" in client_model:
                model = "alibaba/deepseek-v4-flash:free"
            else:
                model = MODEL

        # 确保 session 有效
        if not session.is_valid():
            print("[TH] session 过期, 刷新中...")
            session.refresh()

        # 收集回复 (内部端点 SSE: event: token / data: {...})
        full_text = ""
        events = []

        def on_line(line):
            nonlocal full_text
            events.append(line)
            print(f"[SSE] {line[:200]}", flush=True)
            if line.startswith("data: "):
                try:
                    d = json.loads(line[6:])
                    # 兼容多种字段名
                    for key in ("token", "content", "text", "delta", "message", "chunk"):
                        if key in d and isinstance(d[key], str):
                            full_text += d[key]
                            break
                        if key in d and isinstance(d[key], dict):
                            inner = d[key]
                            for k2 in ("content", "text", "token"):
                                if k2 in inner and isinstance(inner[k2], str):
                                    full_text += inner[k2]
                                    break
                            break
                except Exception:
                    pass

        try:
            session.chat_stream(content, model, on_line)
        except urllib.error.HTTPError as e:
            self._send_json(502, {"error": {"message": f"Upstream {e.code}: {e.read()[:200]}"}})
            return
        except Exception as e:
            # 可能是 session 失效, 刷新重试一次
            print(f"[TH] 转发失败({e}), 刷新 session 重试...")
            try:
                session.refresh()
                full_text = ""
                events = []
                session.chat_stream(content, model, on_line)
            except Exception as e2:
                self._send_json(502, {"error": {"message": f"Upstream error: {e2}"}})
                return

        if not full_text:
            full_text = "(empty response)"

        if stream:
            # OpenAI 流式格式
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


def main():
    print(f"[TH] Token Harbor web proxy 启动: port={PORT} model={MODEL}")
    print(f"[TH] 登录账号: {EMAIL}")
    session.refresh()
    print("[TH] 初始 session 就绪")

    # 后台定时刷新 (每 25 分钟)
    def bg_refresh():
        while True:
            time.sleep(1500)
            try:
                session.refresh()
            except Exception as e:
                print(f"[TH] 后台刷新失败: {e}")

    threading.Thread(target=bg_refresh, daemon=True).start()

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"[TH] 监听 0.0.0.0:{PORT}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
