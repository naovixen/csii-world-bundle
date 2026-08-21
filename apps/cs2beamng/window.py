"""The window: `pywebview` when it is installed (a native window with the OS web view — WebView2
on Windows, WebKit on macOS, GTK/Qt WebKit on Linux), otherwise the default browser. Both show
the very same UI served by `server.py`; only the frame differs.

`show(url, server)` blocks until the window closes (pywebview) or until the server stops
(browser mode: `/api/quit` from the page, or Ctrl-C). In a native window the server gains a
folder picker (`server.pick_folder`) so the Browse buttons open the OS dialog; a browser has no
such thing, and the UI hides them.
"""

import sys
import threading
import webbrowser

WINDOW_TITLE = "cs2beamng — CS2 World Bundle to BeamNG.drive"


def available():
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    return True


# where the named browsers usually live; `webbrowser` itself only knows the system default
BROWSER_CANDIDATES = {
    "chrome": [
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ],
    "edge": [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ],
    "firefox": [
        r"%ProgramFiles%\Mozilla Firefoxirefox.exe",
        r"%ProgramFiles(x86)%\Mozilla Firefoxirefox.exe",
        "/usr/bin/firefox",
        "/Applications/Firefox.app/Contents/MacOS/firefox",
    ],
}


def show(url, server, mode="window", open_ui=True):
    """`mode`: "window" (pywebview if available, else the default browser), or a browser name
    from BROWSER_CANDIDATES / "default"."""
    if not open_ui:
        return _show_browser(url, server, open_browser=False)
    if mode == "window" and available():
        return _show_native(url, server)
    return _show_browser(url, server, name=mode if mode != "window" else "default")


def _open_in(name, url):
    """Open `url` in the named browser; falls back to the system default. Returns what it used."""
    import os
    import subprocess

    if name in BROWSER_CANDIDATES:
        for cand in _registry_app_paths(name) + BROWSER_CANDIDATES[name]:
            exe = os.path.expandvars(cand)
            if os.path.isfile(exe):
                subprocess.Popen([exe, url])
                return name
        print(f"{name} not found on this machine; using the system default browser", flush=True)
    webbrowser.open(url)
    return "default"


def _registry_app_paths(name):
    """Windows: the browser's registered executable (App Paths), for installs outside the usual
    folders. Empty elsewhere."""
    exe = {"chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe"}.get(name)
    if not exe or not sys.platform.startswith("win"):
        return []
    try:
        import winreg
    except ImportError:
        return []
    out = []
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(
                root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
            ) as key:
                value, _kind = winreg.QueryValueEx(key, None)
                if value:
                    out.append(str(value).strip('"'))
        except OSError:
            continue
    return out


def _show_browser(url, server, open_browser=True, name="default"):
    if open_browser:
        used = _open_in(name, url)
        print(
            f"UI opened in {'the system default browser' if used == 'default' else used}. Ctrl-C here quits.",
            flush=True,
        )
    else:
        print(
            "serving only (--no-open): open the URL above in a browser of your choice. Ctrl-C here quits.",
            flush=True,
        )
    try:
        while not server.stopped.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    return 0


def _show_native(url, server):
    import webview

    window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=1280,
        height=860,
        min_size=(960, 640),
        background_color="#0f1115",
        text_select=True,
    )

    def pick_folder(current=None):
        try:
            result = window.create_file_dialog(webview.FOLDER_DIALOG, directory=current or "")
        except Exception:
            return None
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result

    server.pick_folder = pick_folder

    def on_closing():
        # Closing the window while a build runs cancels it (app.launch's shutdown then waits for
        # the worker). No native confirm here: pywebview's cocoa backend blocks the main thread
        # for its dialogs and `closing` runs ON the main thread - a confirm would deadlock macOS.
        # The page's own Quit button asks first; the window's close box is the "I mean it" path.
        if server.runner.running():
            server.runner.cancel()
        return True

    def on_quit_request():
        # the page asked to quit (/api/quit): close the window from a worker thread
        threading.Thread(target=window.destroy, daemon=True).start()

    window.events.closing += on_closing
    server.on_quit = on_quit_request

    def stop_server_when_closed():
        server.stop()

    window.events.closed += stop_server_when_closed
    webview.start(debug=False)  # pywebview picks the platform backend (GTK/Qt on Linux)
    return 0
