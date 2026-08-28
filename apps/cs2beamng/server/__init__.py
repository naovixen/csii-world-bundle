"""The local web server behind the UI: static files, a JSON API and one Server-Sent-Events
stream. Standard library only (`http.server.ThreadingHTTPServer`), bound to 127.0.0.1 on an
ephemeral port, so it is the same on Windows, Linux and macOS and needs no build step.

    server = AppServer()            # owns a Runner, the settings, and the derived-data caches
    url = server.start()            # "http://127.0.0.1:<port>/?t=<token>"
    ...
    server.stop()

Endpoints (all under the token — see below):

    GET  /                        the UI (index.html with the token injected)
    GET  /static/<file>           the UI's ES modules and style sheets
    GET  /events?since=<seq>      SSE: `state` every 0.5 s (no log), `log` deltas, `bye` on quit
    GET  /api/state               the runner state incl. the log (polling fallback)
    GET  /api/settings            {values, schema, sections, basics, presets, problems, detected, canBrowse}
    POST /api/settings            {values: {key: value}} -> merged, coerced, validated, saved
    POST /api/preset              {name} -> applied and saved; "custom" restores the remembered set
    GET  /api/census              census for the current settings (cached by fingerprint)
    GET  /api/map                 the settings screen's map: georeference, suggestions, points
    GET  /api/map.png             the map picture itself (drawn once per bundle, then cached)
    POST /api/poi                 {points: [...]} -> stored, and `poi_file` pointed at them
    POST /api/poi/snap            {at: [x, z]} -> the nearest drivable point, in bundle metres
    POST /api/start               start a build with the current settings
    POST /api/cancel              cancel the running build
    GET  /api/report              the last build's report (or the report file in the level dir)
    POST /api/open                {what: level|output|bundle|derived|report} -> file manager
    POST /api/browse              {key, current} -> native folder picker (only in a window)
    POST /api/installmod          unpack the bundled ripper mod into the CS2 mods folder
    POST /api/quit                stop the server (the window/browser flow calls this)

Why a token: the server listens on localhost, and a web page from anywhere can *send* requests
to localhost. Every `/api` and `/events` request must carry the launch token (`X-Token` header,
or `?t=` for `EventSource`, which cannot set headers). A hostile page cannot read the token —
`/` is same-origin only (no CORS headers) — and cannot send the header without a preflight we
never answer, so it cannot start, cancel or reconfigure a build.

Inside:

* `routes` — the HTTP shell. One route table, the token check, and how a reply is written.
* `app` — `AppServer`: what the UI can ask for, and the settings it holds while asking.
* `caches` — the five expensive derived answers, memoised, three behind a background thread.
* `mapview` / `mapimage` / `mappoints` — the map the region and the fast-travel points are
  picked on: the document, the picture, and the two things drawn on top of it.
"""

from .app import AppServer

__all__ = ["AppServer"]
