/**
 * Pure utility functions for the YouTube Video Intelligence frontend.
 *
 * Extracted from index.html to enable unit testing with Node.js built-in
 * test runner (`node --test`). No DOM or React dependencies.
 */

/**
 * Format a view count into a human-readable string.
 * @param {number} views - Raw view count.
 * @returns {string} Formatted string (e.g., "1.2M views", "45K views").
 */
function formatViews(views) {
  if (!views && views !== 0) return "";
  if (views >= 1_000_000) return (views / 1_000_000).toFixed(1) + "M views";
  if (views >= 1_000) return Math.floor(views / 1_000) + "K views";
  return views + " views";
}

/**
 * Format a duration in seconds into MM:SS string.
 * @param {number} seconds - Duration in seconds.
 * @returns {string} Formatted duration (e.g., "5:03").
 */
function formatDuration(seconds) {
  if (!seconds && seconds !== 0) return "";
  const mins = Math.floor(seconds / 60);
  const secs = String(seconds % 60).padStart(2, "0");
  return `${mins}:${secs}`;
}

/**
 * Parse a single SSE data line into an event object.
 * @param {string} line - Raw SSE line (e.g., 'data: {"type":"status",...}').
 * @returns {object|null} Parsed event object, or null if not a data line.
 */
function parseSSELine(line) {
  if (!line || !line.startsWith("data: ")) return null;
  try {
    return JSON.parse(line.slice(6));
  } catch {
    return null;
  }
}

/**
 * Check whether an SSE event is a terminal event (done or error).
 * @param {object} event - Parsed SSE event.
 * @returns {boolean} True if the stream should end.
 */
function isTerminalEvent(event) {
  if (!event || !event.type) return false;
  return event.type === "done" || event.type === "error";
}

/**
 * Map a video history API item into a display-ready object.
 * @param {object} item - API response item from /api/history/videos.
 * @returns {object} Object with id, label, mode, time.
 */
function mapVideoHistoryItem(item) {
  return {
    id: item.id,
    label: item.video_title || "Untitled",
    mode: item.question
      ? `Q: ${item.question.slice(0, 35)}...`
      : "Summary",
    time: item.created_at,
  };
}

/**
 * Map a theme history API item into a display-ready object.
 * @param {object} item - API response item from /api/history/themes.
 * @returns {object} Object with id, label, mode, time.
 */
function mapThemeHistoryItem(item) {
  return {
    id: item.id,
    label: item.theme || "Untitled",
    mode: `${item.video_count} videos analyzed`,
    time: item.created_at,
  };
}

/**
 * Build a video metadata string from channel, views, and duration.
 * @param {string} channel - Channel name.
 * @param {number|null} views - View count (optional).
 * @param {number|null} duration - Duration in seconds (optional).
 * @returns {string} Formatted metadata line.
 */
function buildVideoMeta(channel, views, duration) {
  let meta = channel || "";
  if (views) meta += ` | ${formatViews(views)}`;
  if (duration) meta += ` | ${formatDuration(duration)}`;
  return meta;
}

/**
 * Validate the max-duration input from the Theme Explorer filters.
 * Empty input falls back to the 30-minute default.
 * @param {string} input - Raw input value (string from a number input).
 * @returns {{ok: true, value: number} | {ok: false, error: string}}
 */
function validateMaxDuration(input) {
  if (input === "" || input === null || input === undefined) {
    return { ok: true, value: 30 };
  }
  const dur = parseInt(input, 10);
  if (isNaN(dur) || dur < 1 || dur > 180) {
    return {
      ok: false,
      error: "Max video length must be between 1 and 180 minutes.",
    };
  }
  return { ok: true, value: dur };
}

/**
 * Consume one chunk of an SSE stream, returning all complete events parsed
 * out of the combined buffer plus the leftover partial line for the next call.
 * @param {string} buffer - Leftover string from the previous call.
 * @param {string} chunk - New decoded chunk from the reader.
 * @returns {{events: object[], remainder: string}}
 */
function splitSSEStream(buffer, chunk) {
  const lines = (buffer + chunk).split("\n");
  const remainder = lines.pop();
  const events = [];
  for (const line of lines) {
    const event = parseSSELine(line);
    if (event) events.push(event);
  }
  return { events, remainder };
}

/**
 * Translate a Single Video Analyzer SSE event into a state patch.
 * Returns null for unrecognised or no-op events (caller skips).
 * @param {object} event - Parsed SSE event.
 * @returns {object|null} Object with the keys to patch into component state.
 */
function videoEventPatch(event) {
  if (!event || !event.type) return null;
  switch (event.type) {
    case "status":
      return { status: event.message };
    case "metadata":
      return { meta: event };
    case "result":
      return { result: event.markdown, status: "", model: event.model || "" };
    case "error":
      return { error: event.message, status: "" };
    case "done":
      return { loading: false, refreshHistory: true };
    default:
      return null;
  }
}

/**
 * Translate a Theme Explorer SSE event into a state patch.
 * Returns null for unrecognised events or keepalives.
 * @param {object} event - Parsed SSE event.
 * @returns {object|null} Object with the keys to patch into component state.
 */
function themeEventPatch(event) {
  if (!event || !event.type || event.type === "keepalive") return null;
  switch (event.type) {
    case "progress":
      return { progress: { pct: event.pct, message: event.message } };
    case "videos_found":
      return { foundVideos: event.videos };
    case "result":
      return {
        mosaic: event.mosaic || [],
        synthesis: event.synthesis || "",
        progressText: event.progress_text || "",
        progress: null,
        model: event.model || "",
      };
    case "error":
      return {
        error: event.message,
        progress: null,
        finished: true,
        loading: false,
      };
    case "done":
      return { finished: true, loading: false, refreshHistory: true };
    default:
      return null;
  }
}

/**
 * Build the JSON body for POST /api/analyze-video.
 * Trims the URL and tolerates missing question.
 * @param {string} url
 * @param {string} question
 * @returns {{url: string, question: string}}
 */
function buildAnalyzeVideoBody(url, question) {
  return { url: (url || "").trim(), question: question || "" };
}

/**
 * Build the JSON body for POST /api/analyze-theme.
 * @param {string} theme
 * @param {string} dateStart
 * @param {string} dateEnd
 * @param {number} maxDurationMin - already-validated integer minutes
 * @param {string} blacklist
 * @returns {object}
 */
function buildAnalyzeThemeBody(theme, dateStart, dateEnd, maxDurationMin, blacklist) {
  return {
    theme: (theme || "").trim(),
    date_start: dateStart || "",
    date_end: dateEnd || "",
    max_duration_min: maxDurationMin,
    blacklist: blacklist || "",
  };
}

/**
 * Map a /api/history/videos/{id} response into the Single Video tab's view state.
 * @param {object} data - API response payload.
 * @returns {{meta: object, result: string}}
 */
function historyDetailToVideoState(data) {
  return {
    meta: {
      title: data.video_title,
      channel: data.channel,
      thumbnail: data.thumbnail,
      url: data.video_url,
    },
    result: data.markdown,
  };
}

/**
 * Map a /api/history/themes/{id} response into the Theme Explorer's view state.
 * @param {object} data - API response payload.
 * @returns {{mosaic: object[], synthesis: string, foundVideos: object[], progressText: string, progress: null}}
 */
function historyDetailToThemeState(data) {
  return {
    mosaic: data.mosaic || [],
    synthesis: data.synthesis || "",
    foundVideos: [],
    progressText: `${data.video_count} videos analyzed.`,
    progress: null,
  };
}

/**
 * Consume an SSE-streamed Response, calling `onEvent` for each parsed event.
 * Resolves once the stream closes. Errors propagate to the caller's catch.
 * @param {Response} response - fetch response with a streamed body.
 * @param {(event: object) => void} onEvent - per-event handler.
 * @returns {Promise<void>}
 */
async function consumeSSEStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    const split = splitSSEStream(buffer, decoder.decode(value, { stream: true }));
    buffer = split.remainder;
    for (const event of split.events) onEvent(event);
  }
}

/**
 * Apply a state patch by calling each setter whose key appears in the patch.
 * Used to bridge pure event-to-patch helpers (e.g. videoEventPatch) into a
 * React component's `useState` setters without spelling out the dispatch
 * inline. Keys not present in `setters` are ignored, letting callers handle
 * non-state signal fields like `refreshHistory` separately.
 * @param {Record<string, (value: any) => void>} setters - map of state key to React setter.
 * @param {object|null|undefined} patch - state patch (returned by an event-patch helper).
 */
function applyPatch(setters, patch) {
  if (!patch) return;
  for (const [key, setter] of Object.entries(setters)) {
    if (key in patch) setter(patch[key]);
  }
}

// Export for Node.js testing; no-op in browser
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    formatViews,
    formatDuration,
    parseSSELine,
    isTerminalEvent,
    mapVideoHistoryItem,
    mapThemeHistoryItem,
    buildVideoMeta,
    validateMaxDuration,
    splitSSEStream,
    videoEventPatch,
    themeEventPatch,
    buildAnalyzeVideoBody,
    buildAnalyzeThemeBody,
    historyDetailToVideoState,
    historyDetailToThemeState,
    consumeSSEStream,
    applyPatch,
  };
}
