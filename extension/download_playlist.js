/* =============================================================================
 * download_playlist.js -- Spicetify extension
 * =============================================================================
 *
 * WHAT IT DOES
 *   Adds a "Download Full Playlist to Songs" entry to the right-click context
 *   menu of every playlist and album inside the Spotify desktop client. Clicking
 *   it hands the Spotify URI to a small local HTTP server that this project
 *   runs, which then downloads the whole playlist/album in the background.
 *
 *   Contract with the local server:
 *       POST http://127.0.0.1:5001/playlist  {"uri": "spotify:playlist:..."}
 *            -> 202 {"status":"queued","uri":"...","queued":N}
 *       GET  http://127.0.0.1:5001/health
 *            -> 200 {"status":"ok","queued":N,"version":1}
 *
 *   The Spotify client is an Electron page with a strict Content-Security-Policy
 *   which, depending on the client version, may block plain `fetch` to
 *   127.0.0.1. This extension therefore tries `fetch` first and transparently
 *   falls back to `Spicetify.CosmosAsync`. If both transports fail, it tells you
 *   so, prints the command to start the server, and copies the URI to your
 *   clipboard so you can paste it into the CLI by hand.
 *
 * REQUIREMENT
 *   The local server must be running:
 *
 *       python main.py --serve
 *
 * WHERE TO INSTALL
 *   Run `spicetify path -e root` to get the authoritative directory -- it has
 *   moved between Spicetify versions, so do not hardcode it. Common values:
 *     macOS / Linux : ~/.spicetify/Extensions/       (Spicetify 2.x)
 *                     ~/.config/spicetify/Extensions/ (older layouts)
 *     Windows       : %APPDATA%\spicetify\Extensions\
 *
 *   (Run `spicetify path userdata` if you are unsure where that folder lives.)
 *
 * HOW TO ENABLE
 *       spicetify config extensions download_playlist.js
 *       spicetify apply
 *
 *   After `spicetify apply` the Spotify client restarts. Open the developer
 *   console (Spicetify enables it via `spicetify enable-devtools`) and look for
 *   the "[download_playlist]" log lines to confirm the extension loaded and
 *   whether the backend answered the startup health probe.
 *
 * =============================================================================
 */

(function downloadPlaylistExtension() {
    "use strict";

    /* ---------------------------------------------------------------------- */
    /* Configuration                                                          */
    /* ---------------------------------------------------------------------- */

    const SERVER_ORIGIN = "http://127.0.0.1:5001";
    const QUEUE_ENDPOINT = SERVER_ORIGIN + "/playlist";
    const HEALTH_ENDPOINT = SERVER_ORIGIN + "/health";

    const MENU_ITEM_NAME = "Download Full Playlist to Songs";
    const MENU_ITEM_ICON = "download";

    const POST_TIMEOUT_MS = 10000;
    const HEALTH_TIMEOUT_MS = 4000;

    const READY_POLL_INTERVAL_MS = 300;
    const READY_TIMEOUT_MS = 30000;

    const SERVE_COMMAND = "python main.py --serve";
    const LOG_PREFIX = "[download_playlist]";

    /**
     * Lowercase URI types this menu item is offered for. Spicetify's
     * `Spicetify.URI.Type` enum has been renamed more than once across client
     * versions, so the raw strings are the authoritative fallback and the enum
     * values (when present) are merged in on top of them.
     */
    const SUPPORTED_TYPE_STRINGS = ["playlist", "playlist-v2", "album"];

    /** URIs currently being POSTed, used to swallow impatient double-clicks. */
    const inFlightUris = new Set();

    /* ---------------------------------------------------------------------- */
    /* Small helpers                                                          */
    /* ---------------------------------------------------------------------- */

    function log(...args) {
        console.log(LOG_PREFIX, ...args);
    }

    function warn(...args) {
        console.warn(LOG_PREFIX, ...args);
    }

    /**
     * Show a Spicetify toast, never throwing if the API happens to be missing.
     * @param {string} message
     * @param {boolean} [isError=false]
     */
    function notify(message, isError = false) {
        try {
            Spicetify.showNotification(message, isError);
        } catch (error) {
            warn("showNotification failed:", error);
        }
    }

    /**
     * True once every Spicetify sub-API this extension depends on exists.
     * @returns {boolean}
     */
    function isSpicetifyReady() {
        return (
            typeof Spicetify !== "undefined" &&
            Boolean(Spicetify) &&
            typeof Spicetify.showNotification === "function" &&
            Boolean(Spicetify.URI) &&
            typeof Spicetify.URI.fromString === "function" &&
            Boolean(Spicetify.ContextMenu) &&
            typeof Spicetify.ContextMenu.Item === "function"
        );
    }

    /**
     * Poll until Spicetify has finished booting. Extensions are evaluated before
     * the client wires up its APIs, so this is not optional.
     * @returns {Promise<boolean>} true if ready, false if it timed out.
     */
    function waitForSpicetify() {
        return new Promise((resolve) => {
            if (isSpicetifyReady()) {
                resolve(true);
                return;
            }

            const deadline = Date.now() + READY_TIMEOUT_MS;
            const timer = setInterval(() => {
                if (isSpicetifyReady()) {
                    clearInterval(timer);
                    resolve(true);
                } else if (Date.now() >= deadline) {
                    clearInterval(timer);
                    resolve(false);
                }
            }, READY_POLL_INTERVAL_MS);
        });
    }

    /**
     * Normalise whatever the context menu handed us into an array of strings.
     * Spicetify passes an array of URIs, but be liberal about a bare string.
     * @param {string|string[]|unknown} input
     * @returns {string[]}
     */
    function toUriList(input) {
        if (typeof input === "string") {
            return [input];
        }
        if (Array.isArray(input)) {
            return input.filter((entry) => typeof entry === "string");
        }
        return [];
    }

    /**
     * The set of accepted URI types, built from the hardcoded strings plus
     * whatever `Spicetify.URI.Type` currently calls playlists and albums.
     * @returns {Set<string>}
     */
    function supportedTypes() {
        const types = new Set(SUPPORTED_TYPE_STRINGS);
        try {
            const enumeration = Spicetify?.URI?.Type;
            if (enumeration) {
                for (const key of ["PLAYLIST", "PLAYLIST_V2", "ALBUM"]) {
                    const value = enumeration[key];
                    if (typeof value === "string" && value.length > 0) {
                        types.add(value.toLowerCase());
                    }
                }
            }
        } catch (error) {
            // A missing/odd enum is fine -- the string fallbacks still apply.
        }
        return types;
    }

    /**
     * Last-ditch type sniffing straight off the URI string, used when
     * `Spicetify.URI.fromString` throws or hands back something typeless.
     * @param {string} uri
     * @returns {string} lowercase type, or "" when it cannot be determined.
     */
    function typeFromRawUri(uri) {
        if (typeof uri !== "string") {
            return "";
        }
        const parts = uri.split(":");
        if (parts.length < 3 || parts[0] !== "spotify") {
            return "";
        }
        return String(parts[1] ?? "").toLowerCase();
    }

    /**
     * Should the menu item appear for this URI? Must never throw: it runs inside
     * Spotify's own render path, and an exception there breaks the whole menu.
     * @param {string} uri
     * @returns {boolean}
     */
    function isSupportedUri(uri) {
        if (typeof uri !== "string" || uri.length === 0) {
            return false;
        }

        const accepted = supportedTypes();

        try {
            const parsed = Spicetify.URI.fromString(uri);
            const parsedType = parsed?.type;
            if (typeof parsedType === "string" && parsedType.length > 0) {
                return accepted.has(parsedType.toLowerCase());
            }
        } catch (error) {
            // Fall through to raw string sniffing below.
        }

        return accepted.has(typeFromRawUri(uri));
    }

    /**
     * Context menu predicate. True only when every clicked URI is a playlist or
     * an album (Spotify allows multi-select in some views).
     * @param {string[]} uris
     * @returns {boolean}
     */
    function shouldAddMenuItem(uris) {
        try {
            const list = toUriList(uris);
            return list.length > 0 && list.every(isSupportedUri);
        } catch (error) {
            warn("shouldAdd predicate failed, hiding menu item:", error);
            return false;
        }
    }

    /* ---------------------------------------------------------------------- */
    /* Transport                                                              */
    /* ---------------------------------------------------------------------- */

    /**
     * Read a JSON body without exploding on an empty or non-JSON response.
     * @param {Response} response
     * @returns {Promise<Object>}
     */
    async function readJsonBody(response) {
        try {
            const body = await response.json();
            return body && typeof body === "object" ? body : {};
        } catch (error) {
            return {};
        }
    }

    /**
     * Transport #1: plain `fetch`, bounded by a 10 s AbortController so a hung
     * socket cannot make the menu item feel dead.
     * @param {string} uri
     * @returns {Promise<Object>} the parsed server response
     */
    async function postViaFetch(uri) {
        if (typeof fetch !== "function" || typeof AbortController !== "function") {
            throw new Error("fetch/AbortController unavailable in this client");
        }

        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), POST_TIMEOUT_MS);

        try {
            const response = await fetch(QUEUE_ENDPOINT, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    Accept: "application/json"
                },
                body: JSON.stringify({ uri }),
                signal: controller.signal
            });

            if (!response || !response.ok) {
                throw new Error("server replied HTTP " + (response?.status ?? "?"));
            }

            return await readJsonBody(response);
        } finally {
            clearTimeout(timeout);
        }
    }

    /**
     * Transport #2: Spicetify's own Cosmos bridge, which is not subject to the
     * page CSP and therefore usually works when `fetch` is blocked.
     * @param {string} uri
     * @returns {Promise<Object>} the parsed server response
     */
    async function postViaCosmos(uri) {
        const cosmos = Spicetify?.CosmosAsync;
        if (!cosmos || typeof cosmos.post !== "function") {
            throw new Error("Spicetify.CosmosAsync.post unavailable");
        }
        const body = await cosmos.post(QUEUE_ENDPOINT, { uri: uri });
        return body && typeof body === "object" ? body : {};
    }

    /**
     * Copy text to the clipboard, guarded -- this is a convenience fallback and
     * must never mask the real error.
     * @param {string} text
     * @returns {Promise<boolean>} whether the copy succeeded
     */
    async function copyToClipboard(text) {
        try {
            const clipboard = Spicetify?.Platform?.ClipboardAPI;
            if (!clipboard || typeof clipboard.copy !== "function") {
                return false;
            }
            await clipboard.copy(text);
            return true;
        } catch (error) {
            warn("clipboard copy failed:", error);
            return false;
        }
    }

    /* ---------------------------------------------------------------------- */
    /* Click handler                                                          */
    /* ---------------------------------------------------------------------- */

    /**
     * Build the success toast, including the queue depth when the server
     * reported one.
     * @param {Object} result
     * @returns {string}
     */
    function queuedMessage(result) {
        const depth = result?.queued;
        if (typeof depth === "number" && Number.isFinite(depth)) {
            return "Queued for download (" + depth + " in queue)";
        }
        return "Queued for download";
    }

    /**
     * Context menu click handler: POST the first clicked URI to the local
     * server, with fetch -> CosmosAsync fallback and loud failure reporting.
     * @param {string[]} uris
     * @returns {Promise<void>}
     */
    async function onMenuItemClick(uris) {
        const uri = toUriList(uris)[0];

        if (!uri) {
            notify("Download failed: no playlist or album URI found", true);
            warn("click with no usable URI:", uris);
            return;
        }

        if (inFlightUris.has(uri)) {
            notify("Already sending this one, hang on...");
            log("ignoring double-click for", uri);
            return;
        }

        inFlightUris.add(uri);

        try {
            let result = null;
            let fetchError = null;

            try {
                result = await postViaFetch(uri);
                log("queued via fetch:", uri, result);
            } catch (error) {
                fetchError = error;
                warn("fetch transport failed (CSP or server down?):", error);
            }

            if (!result) {
                try {
                    result = await postViaCosmos(uri);
                    log("queued via CosmosAsync:", uri, result);
                } catch (cosmosError) {
                    warn("CosmosAsync transport failed:", cosmosError);
                    notify(
                        "Download server not reachable at " +
                            SERVER_ORIGIN +
                            " -- URI copied to clipboard",
                        true
                    );
                    console.error(
                        LOG_PREFIX,
                        "Could not reach the local download server.\n" +
                            "  Start it with:  " + SERVE_COMMAND + "\n" +
                            "  Or queue it manually with the URI:  " + uri + "\n" +
                            "  fetch error:  " + (fetchError?.message ?? fetchError) + "\n" +
                            "  cosmos error: " + (cosmosError?.message ?? cosmosError)
                    );
                    const copied = await copyToClipboard(uri);
                    log(copied ? "URI copied to clipboard" : "clipboard unavailable");
                    return;
                }
            }

            notify(queuedMessage(result));
        } finally {
            inFlightUris.delete(uri);
        }
    }

    /* ---------------------------------------------------------------------- */
    /* Startup health probe                                                   */
    /* ---------------------------------------------------------------------- */

    /**
     * One-shot GET /health on load. Console only, deliberately: a toast here
     * would fire on every single Spotify launch.
     * @returns {Promise<void>}
     */
    async function probeHealth() {
        try {
            let body = null;

            if (typeof fetch === "function" && typeof AbortController === "function") {
                const controller = new AbortController();
                const timeout = setTimeout(() => controller.abort(), HEALTH_TIMEOUT_MS);
                try {
                    const response = await fetch(HEALTH_ENDPOINT, {
                        method: "GET",
                        headers: { Accept: "application/json" },
                        signal: controller.signal
                    });
                    if (response?.ok) {
                        body = await readJsonBody(response);
                    }
                } catch (error) {
                    // Fall through to the Cosmos attempt below.
                } finally {
                    clearTimeout(timeout);
                }
            }

            if (!body && typeof Spicetify?.CosmosAsync?.get === "function") {
                try {
                    body = await Spicetify.CosmosAsync.get(HEALTH_ENDPOINT);
                } catch (error) {
                    body = null;
                }
            }

            if (body) {
                log(
                    "backend is UP at " + SERVER_ORIGIN,
                    "status=" + (body.status ?? "?"),
                    "queued=" + (body.queued ?? "?"),
                    "version=" + (body.version ?? "?")
                );
            } else {
                log(
                    "backend is DOWN at " + SERVER_ORIGIN + " -- " +
                        "start it with `" + SERVE_COMMAND + "` before downloading."
                );
            }
        } catch (error) {
            warn("health probe blew up unexpectedly:", error);
        }
    }

    /* ---------------------------------------------------------------------- */
    /* Bootstrap                                                              */
    /* ---------------------------------------------------------------------- */

    /**
     * Register the context menu item and kick off the health probe.
     * @returns {void}
     */
    function init() {
        try {
            const item = new Spicetify.ContextMenu.Item(
                MENU_ITEM_NAME,
                onMenuItemClick,
                shouldAddMenuItem,
                MENU_ITEM_ICON
            );
            item.register();
            log('registered context menu item "' + MENU_ITEM_NAME + '"');
        } catch (error) {
            console.error(LOG_PREFIX, "failed to register context menu item:", error);
            return;
        }

        probeHealth();
    }

    waitForSpicetify().then((ready) => {
        if (!ready) {
            console.warn(
                LOG_PREFIX,
                "gave up waiting for Spicetify after " +
                    Math.round(READY_TIMEOUT_MS / 1000) +
                    "s -- the context menu item was NOT registered. " +
                    "Check `spicetify apply` and that this file is listed in " +
                    "`spicetify config extensions`."
            );
            return;
        }
        init();
    });
})();
