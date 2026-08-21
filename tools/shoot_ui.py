"""Look at the application's own UI: screenshot it, and run JavaScript in it.

    python -m cs2beamng gui --no-open --port 8791        # prints a URL with a token
    python tools/shoot_ui.py "<url>" out.png 1440 1500 4 "<optional js>"

A browser's own `--screenshot` flag waits for the page to go quiet and this page holds an SSE
stream open for ever, so it never fires - it hangs until it is killed. The DevTools protocol
does not care: navigate, wait, evaluate, capture. `captureBeyondViewport` gets the whole page,
not just the fold, so a settings screen can be read in one image.

The optional JavaScript is evaluated before the capture and its value printed, which is what
makes a section, a search or a badge reachable without a human at the keyboard - pass a Promise
to await something. Nothing here is part of the application; it is how the person building the
UI checks what they built instead of describing it.

Needs a Chromium browser (Edge is looked for first) and nothing else - the WebSocket client is
the four frames of RFC 6455 this uses, so there is no dependency to install.
"""

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]
HERE = os.path.dirname(os.path.abspath(__file__))


def find_browser():
    for path in BROWSERS:
        if os.path.isfile(path):
            return path
    raise SystemExit("no Chromium-based browser found; edit BROWSERS")


class WS:
    """The four frames of RFC 6455 this needs: text out, text in, unmasked server frames."""

    def __init__(self, url):
        rest = url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)))
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.next_id = 0

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, method, **params):
        self.next_id += 1
        body = json.dumps({"id": self.next_id, "method": method, "params": params}).encode()
        mask = os.urandom(4)
        head = b"\x81"
        n = len(body)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += b"\xfe" + struct.pack(">H", n)
        else:
            head += b"\xff" + struct.pack(">Q", n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(body)))
        return self.next_id

    def recv(self):
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if (b0 & 0x0F) == 1:
                return json.loads(payload)

    def call(self, method, **params):
        want = self.send(method, **params)
        while True:
            msg = self.recv()
            if msg.get("id") == want:
                return msg


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    url, out = sys.argv[1], sys.argv[2]
    width = int(sys.argv[3]) if len(sys.argv) > 3 else 1400
    height = int(sys.argv[4]) if len(sys.argv) > 4 else 1700
    wait = float(sys.argv[5]) if len(sys.argv) > 5 else 3.0
    script = sys.argv[6] if len(sys.argv) > 6 else None

    profile = os.path.join(tempfile.gettempdir(), "cs2beamng_shoot_ui_profile")
    proc = subprocess.Popen(
        [
            find_browser(),
            "--headless=new",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            f"--window-size={width},{height}",
            "--remote-debugging-port=9223",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(60):
            try:
                pages = json.loads(
                    urllib.request.urlopen("http://127.0.0.1:9223/json/list", timeout=1).read()
                )
                target = next((p for p in pages if p.get("type") == "page"), None)
                if target:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not target:
            raise SystemExit("no devtools target")
        ws = WS(target["webSocketDebuggerUrl"])
        ws.call("Page.enable")
        ws.call(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=1,
            mobile=False,
        )
        ws.call("Page.navigate", url=url)
        time.sleep(wait)
        if script:
            res = ws.call(
                "Runtime.evaluate", expression=script, returnByValue=True, awaitPromise=True
            )
            print("js:", json.dumps(res.get("result", {}).get("result", {}).get("value"))[:2000])
            time.sleep(1.0)
        shot = ws.call("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        data = base64.b64decode(shot["result"]["data"])
        with open(out, "wb") as handle:
            handle.write(data)
        print(f"wrote {out} ({len(data):,} bytes)")
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
