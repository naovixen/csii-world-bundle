"""The window: `pywebview` when it is installed (a native window with the OS web view — WebView2
on Windows, WebKit on macOS, GTK/Qt WebKit on Linux), otherwise the default browser. Both show
the very same UI served by `server.py`; only the frame differs.

`show(url, server)` blocks until the window closes (pywebview) or until the server stops
(browser mode: `/api/quit` from the page, or Ctrl-C). In a native window the server gains a
folder picker (`server.pick_folder`) so the Browse buttons open the OS dialog; a browser has no
such thing, and the UI hides them.
"""

import threading
import webbrowser

WINDOW_TITLE = "cs2beamng — CS2 World Bundle to BeamNG.drive"


def available():
    try:
        import webview  # noqa: F401,WPS433
    except Exception:  # noqa: BLE001 - any import failure means "no native window here"
        return False
    return True


def show(url, server, force_browser=False):
    if not force_browser and available():
        return _show_native(url, server)
    return _show_browser(url, server)


def _show_browser(url, server):
    print("no native window (pip install -e .[gui] for one); opening the default browser. Ctrl-C here quits.", flush=True)
    webbrowser.open(url)
    try:
        while not server.stopped.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    return 0


def _show_native(url, server):
    import webview  # noqa: WPS433

    window = webview.create_window(WINDOW_TITLE, url, width=1280, height=860, min_size=(960, 640),
                                   background_color="#0f1115", text_select=True)

    def pick_folder(current=None):
        try:
            result = window.create_file_dialog(webview.FOLDER_DIALOG, directory=current or "")
        except Exception:  # noqa: BLE001
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
    webview.start(debug=False)                        # pywebview picks the platform backend (GTK/Qt on Linux)
    return 0
