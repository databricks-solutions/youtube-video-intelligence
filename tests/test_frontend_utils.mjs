/**
 * Frontend utility tests using Node.js built-in test runner.
 * Run with: node --test tests/test_frontend_utils.mjs
 *
 * No npm install needed.
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const utils = require("../app/static/utils.js");

const {
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
} = utils;

// --- formatViews ---

describe("formatViews", () => {
  it("formats millions", () => {
    assert.equal(formatViews(1_500_000), "1.5M views");
  });

  it("formats exact million", () => {
    assert.equal(formatViews(1_000_000), "1.0M views");
  });

  it("formats thousands", () => {
    assert.equal(formatViews(45_000), "45K views");
  });

  it("floors thousands", () => {
    assert.equal(formatViews(1_999), "1K views");
  });

  it("formats small numbers", () => {
    assert.equal(formatViews(42), "42 views");
  });

  it("formats zero", () => {
    assert.equal(formatViews(0), "0 views");
  });

  it("returns empty for null and undefined", () => {
    assert.equal(formatViews(null), "");
    assert.equal(formatViews(undefined), "");
  });
});

// --- formatDuration ---

describe("formatDuration", () => {
  it("formats minutes and seconds", () => {
    assert.equal(formatDuration(303), "5:03");
  });

  it("pads single-digit seconds", () => {
    assert.equal(formatDuration(61), "1:01");
  });

  it("formats zero", () => {
    assert.equal(formatDuration(0), "0:00");
  });

  it("formats seconds only", () => {
    assert.equal(formatDuration(45), "0:45");
  });

  it("returns empty for null", () => {
    assert.equal(formatDuration(null), "");
  });

  it("formats long videos", () => {
    assert.equal(formatDuration(3661), "61:01");
  });
});

// --- parseSSELine ---

describe("parseSSELine", () => {
  it("parses valid data line", () => {
    const event = parseSSELine('data: {"type":"status","message":"Loading"}');
    assert.deepEqual(event, { type: "status", message: "Loading" });
  });

  it("returns null for comment lines", () => {
    assert.equal(parseSSELine(": keepalive"), null);
  });

  it("returns null for falsy input", () => {
    assert.equal(parseSSELine(""), null);
    assert.equal(parseSSELine(null), null);
    assert.equal(parseSSELine(undefined), null);
  });

  it("returns null for malformed JSON", () => {
    assert.equal(parseSSELine("data: {not json}"), null);
  });

  it("returns null for non-data lines", () => {
    assert.equal(parseSSELine("event: message"), null);
  });
});

// --- isTerminalEvent ---

describe("isTerminalEvent", () => {
  it("done is terminal", () => {
    assert.equal(isTerminalEvent({ type: "done" }), true);
  });

  it("error is terminal", () => {
    assert.equal(isTerminalEvent({ type: "error", message: "fail" }), true);
  });

  it("status is not terminal", () => {
    assert.equal(isTerminalEvent({ type: "status" }), false);
  });

  it("progress is not terminal", () => {
    assert.equal(isTerminalEvent({ type: "progress", pct: 50 }), false);
  });

  it("null and event-without-type are not terminal", () => {
    assert.equal(isTerminalEvent(null), false);
    assert.equal(isTerminalEvent({}), false);
  });
});

// --- mapVideoHistoryItem ---

describe("mapVideoHistoryItem", () => {
  it("maps summary item", () => {
    const item = {
      id: "uuid-1",
      video_title: "My Video",
      created_at: "2026-03-23 12:00",
    };
    const result = mapVideoHistoryItem(item);
    assert.equal(result.id, "uuid-1");
    assert.equal(result.label, "My Video");
    assert.equal(result.mode, "Summary");
    assert.equal(result.time, "2026-03-23 12:00");
  });

  it("maps question item with truncation", () => {
    const item = {
      id: "uuid-2",
      video_title: "Test",
      question: "What is the meaning of life in this video context here?",
      created_at: "2026-03-23",
    };
    const result = mapVideoHistoryItem(item);
    assert.ok(result.mode.startsWith("Q: "));
    assert.ok(result.mode.endsWith("..."));
    assert.ok(result.mode.length <= 42); // "Q: " + 35 chars + "..."
  });

  it("falls back to Untitled", () => {
    const result = mapVideoHistoryItem({ id: "x", created_at: "now" });
    assert.equal(result.label, "Untitled");
  });
});

// --- mapThemeHistoryItem ---

describe("mapThemeHistoryItem", () => {
  it("maps theme item", () => {
    const item = {
      id: "uuid-3",
      theme: "Battle of Hastings",
      video_count: 10,
      created_at: "2026-03-23 13:00",
    };
    const result = mapThemeHistoryItem(item);
    assert.equal(result.label, "Battle of Hastings");
    assert.equal(result.mode, "10 videos analyzed");
  });

  it("falls back to Untitled", () => {
    const result = mapThemeHistoryItem({ id: "x", video_count: 0, created_at: "now" });
    assert.equal(result.label, "Untitled");
  });
});

// --- buildVideoMeta ---

describe("buildVideoMeta", () => {
  it("combines all fields", () => {
    const meta = buildVideoMeta("TestChannel", 1_500_000, 303);
    assert.equal(meta, "TestChannel | 1.5M views | 5:03");
  });

  it("channel only", () => {
    assert.equal(buildVideoMeta("Ch", null, null), "Ch");
  });

  it("channel and views", () => {
    assert.equal(buildVideoMeta("Ch", 500, null), "Ch | 500 views");
  });

  it("channel and duration", () => {
    assert.equal(buildVideoMeta("Ch", null, 90), "Ch | 1:30");
  });

  it("empty channel", () => {
    assert.equal(buildVideoMeta("", 1000, 60), " | 1K views | 1:00");
  });
});

// --- validateMaxDuration ---

describe("validateMaxDuration", () => {
  it("accepts a valid integer", () => {
    assert.deepEqual(validateMaxDuration("45"), { ok: true, value: 45 });
  });

  it("falls back to 30 on empty string", () => {
    assert.deepEqual(validateMaxDuration(""), { ok: true, value: 30 });
  });

  it("falls back to 30 on null/undefined", () => {
    assert.deepEqual(validateMaxDuration(null), { ok: true, value: 30 });
    assert.deepEqual(validateMaxDuration(undefined), { ok: true, value: 30 });
  });

  it("rejects below the lower bound", () => {
    const r = validateMaxDuration("0");
    assert.equal(r.ok, false);
    assert.match(r.error, /1 and 180/);
  });

  it("rejects above the upper bound", () => {
    const r = validateMaxDuration("181");
    assert.equal(r.ok, false);
  });

  it("rejects non-numeric input", () => {
    const r = validateMaxDuration("abc");
    assert.equal(r.ok, false);
  });

  it("accepts boundaries", () => {
    assert.deepEqual(validateMaxDuration("1"), { ok: true, value: 1 });
    assert.deepEqual(validateMaxDuration("180"), { ok: true, value: 180 });
  });

  it("truncates a float input via parseInt", () => {
    assert.deepEqual(validateMaxDuration("30.7"), { ok: true, value: 30 });
  });
});

// --- splitSSEStream ---

describe("splitSSEStream", () => {
  it("parses one complete event", () => {
    const out = splitSSEStream("", 'data: {"type":"status","message":"hi"}\n');
    assert.deepEqual(out.events, [{ type: "status", message: "hi" }]);
    assert.equal(out.remainder, "");
  });

  it("preserves the partial trailing line as remainder", () => {
    const out = splitSSEStream("", 'data: {"type":"a"}\ndata: {"type":');
    assert.deepEqual(out.events, [{ type: "a" }]);
    assert.equal(out.remainder, 'data: {"type":');
  });

  it("joins a previous remainder with the new chunk", () => {
    const out = splitSSEStream('data: {"type":"b', '"}\n');
    assert.deepEqual(out.events, [{ type: "b" }]);
    assert.equal(out.remainder, "");
  });

  it("ignores blank and non-data lines", () => {
    const out = splitSSEStream("", '\nevent: ping\ndata: {"type":"c"}\n');
    assert.deepEqual(out.events, [{ type: "c" }]);
  });

  it("handles multiple events in one chunk", () => {
    const out = splitSSEStream(
      "",
      'data: {"type":"a"}\ndata: {"type":"b"}\n',
    );
    assert.deepEqual(out.events, [{ type: "a" }, { type: "b" }]);
    assert.equal(out.remainder, "");
  });

  it("returns empty events on an empty chunk with no remainder", () => {
    const out = splitSSEStream("", "");
    assert.deepEqual(out.events, []);
    assert.equal(out.remainder, "");
  });

  it("skips data lines whose JSON is malformed", () => {
    const out = splitSSEStream(
      "",
      'data: {not json}\ndata: {"type":"ok"}\n',
    );
    assert.deepEqual(out.events, [{ type: "ok" }]);
  });
});

// --- videoEventPatch ---

describe("videoEventPatch", () => {
  it("returns null for missing event or type", () => {
    assert.equal(videoEventPatch(null), null);
    assert.equal(videoEventPatch({}), null);
  });

  it("status patches the status field", () => {
    assert.deepEqual(
      videoEventPatch({ type: "status", message: "loading" }),
      { status: "loading" },
    );
  });

  it("metadata patches the meta field with the whole event", () => {
    const ev = { type: "metadata", title: "T", channel: "C" };
    assert.deepEqual(videoEventPatch(ev), { meta: ev });
  });

  it("result clears status and sets result", () => {
    assert.deepEqual(
      videoEventPatch({ type: "result", markdown: "**hi**" }),
      { result: "**hi**", status: "" },
    );
  });

  it("error clears status and sets error", () => {
    assert.deepEqual(
      videoEventPatch({ type: "error", message: "oops" }),
      { error: "oops", status: "" },
    );
  });

  it("done flips loading off and signals refreshHistory", () => {
    assert.deepEqual(
      videoEventPatch({ type: "done" }),
      { loading: false, refreshHistory: true },
    );
  });

  it("returns null for unknown event types", () => {
    assert.equal(videoEventPatch({ type: "weird" }), null);
  });
});

// --- themeEventPatch ---

describe("themeEventPatch", () => {
  it("returns null for missing event, type, or keepalive", () => {
    assert.equal(themeEventPatch(null), null);
    assert.equal(themeEventPatch({}), null);
    assert.equal(themeEventPatch({ type: "keepalive" }), null);
  });

  it("progress patches progress object", () => {
    assert.deepEqual(
      themeEventPatch({ type: "progress", pct: 40, message: "step 4" }),
      { progress: { pct: 40, message: "step 4" } },
    );
  });

  it("videos_found patches the foundVideos list", () => {
    assert.deepEqual(
      themeEventPatch({ type: "videos_found", videos: [{ url: "u" }] }),
      { foundVideos: [{ url: "u" }] },
    );
  });

  it("result patches mosaic, synthesis, progress text, clears progress", () => {
    assert.deepEqual(
      themeEventPatch({
        type: "result",
        mosaic: [{ url: "x" }],
        synthesis: "summary",
        progress_text: "10 analyzed",
      }),
      {
        mosaic: [{ url: "x" }],
        synthesis: "summary",
        progressText: "10 analyzed",
        progress: null,
      },
    );
  });

  it("result tolerates missing optional fields", () => {
    assert.deepEqual(
      themeEventPatch({ type: "result" }),
      { mosaic: [], synthesis: "", progressText: "", progress: null },
    );
  });

  it("error sets error, clears progress, marks finished and not loading", () => {
    assert.deepEqual(
      themeEventPatch({ type: "error", message: "rate limited" }),
      {
        error: "rate limited",
        progress: null,
        finished: true,
        loading: false,
      },
    );
  });

  it("done marks finished, not loading, and signals refreshHistory", () => {
    assert.deepEqual(
      themeEventPatch({ type: "done" }),
      { finished: true, loading: false, refreshHistory: true },
    );
  });

  it("returns null for unknown event types", () => {
    assert.equal(themeEventPatch({ type: "weird" }), null);
  });
});

// --- buildAnalyzeVideoBody ---

describe("buildAnalyzeVideoBody", () => {
  it("trims the URL and passes the question through", () => {
    assert.deepEqual(
      buildAnalyzeVideoBody("  https://x.com  ", "What about Y?"),
      { url: "https://x.com", question: "What about Y?" },
    );
  });

  it("tolerates missing question", () => {
    assert.deepEqual(buildAnalyzeVideoBody("u", undefined), {
      url: "u",
      question: "",
    });
  });

  it("tolerates missing url", () => {
    assert.deepEqual(buildAnalyzeVideoBody(null, "q"), {
      url: "",
      question: "q",
    });
  });
});

// --- buildAnalyzeThemeBody ---

describe("buildAnalyzeThemeBody", () => {
  it("packs all fields into the API shape", () => {
    assert.deepEqual(
      buildAnalyzeThemeBody(
        " stalingrad ",
        "2024-01-01",
        "2024-12-31",
        45,
        "channel1\nchannel2",
      ),
      {
        theme: "stalingrad",
        date_start: "2024-01-01",
        date_end: "2024-12-31",
        max_duration_min: 45,
        blacklist: "channel1\nchannel2",
      },
    );
  });

  it("substitutes empty strings for missing optionals", () => {
    assert.deepEqual(buildAnalyzeThemeBody("t", "", "", 30, ""), {
      theme: "t",
      date_start: "",
      date_end: "",
      max_duration_min: 30,
      blacklist: "",
    });
  });

  it("tolerates undefined optional inputs", () => {
    assert.deepEqual(
      buildAnalyzeThemeBody("t", undefined, undefined, 30, undefined),
      {
        theme: "t",
        date_start: "",
        date_end: "",
        max_duration_min: 30,
        blacklist: "",
      },
    );
  });
});

// --- historyDetailToVideoState ---

describe("historyDetailToVideoState", () => {
  it("maps API fields into the local view state", () => {
    const view = historyDetailToVideoState({
      video_title: "Title",
      channel: "Ch",
      thumbnail: "https://t",
      video_url: "https://v",
      markdown: "**summary**",
    });
    assert.deepEqual(view, {
      meta: {
        title: "Title",
        channel: "Ch",
        thumbnail: "https://t",
        url: "https://v",
      },
      result: "**summary**",
    });
  });
});

// --- historyDetailToThemeState ---

describe("historyDetailToThemeState", () => {
  it("maps API fields and resets transient state", () => {
    const view = historyDetailToThemeState({
      mosaic: [{ url: "u" }],
      synthesis: "summary",
      video_count: 7,
    });
    assert.deepEqual(view, {
      mosaic: [{ url: "u" }],
      synthesis: "summary",
      foundVideos: [],
      progressText: "7 videos analyzed.",
      progress: null,
    });
  });

  it("defaults to empty mosaic and empty synthesis", () => {
    const view = historyDetailToThemeState({ video_count: 0 });
    assert.deepEqual(view, {
      mosaic: [],
      synthesis: "",
      foundVideos: [],
      progressText: "0 videos analyzed.",
      progress: null,
    });
  });
});

// --- consumeSSEStream ---

function fakeSSEResponse(...chunks) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return { body: stream };
}

describe("consumeSSEStream", () => {
  it("emits one event per parsed data line", async () => {
    const events = [];
    await consumeSSEStream(
      fakeSSEResponse(
        'data: {"type":"a"}\n\ndata: {"type":"b"}\n\n',
      ),
      e => events.push(e),
    );
    assert.deepEqual(events, [{ type: "a" }, { type: "b" }]);
  });

  it("reassembles events split across chunks", async () => {
    const events = [];
    await consumeSSEStream(
      fakeSSEResponse(
        'data: {"type":"a","msg":"hi"',
        '}\n\ndata: {"type":"b"}\n\n',
      ),
      e => events.push(e),
    );
    assert.deepEqual(events, [{ type: "a", msg: "hi" }, { type: "b" }]);
  });

  it("ignores non-data SSE lines", async () => {
    const events = [];
    await consumeSSEStream(
      fakeSSEResponse(
        ': comment\nevent: ping\ndata: {"type":"a"}\n\n',
      ),
      e => events.push(e),
    );
    assert.deepEqual(events, [{ type: "a" }]);
  });

  it("resolves cleanly on an empty stream", async () => {
    const events = [];
    await consumeSSEStream(fakeSSEResponse(), e => events.push(e));
    assert.deepEqual(events, []);
  });

  it("propagates handler exceptions to the caller", async () => {
    const boom = new Error("boom");
    await assert.rejects(
      consumeSSEStream(
        fakeSSEResponse('data: {"type":"a"}\n\n'),
        () => { throw boom; },
      ),
      err => err === boom,
    );
  });
});

// --- applyPatch ---

describe("applyPatch", () => {
  function captureSetters() {
    const calls = [];
    const make = (key) => (value) => calls.push([key, value]);
    return { make, calls };
  }

  it("calls each setter whose key is in the patch", () => {
    const { make, calls } = captureSetters();
    applyPatch(
      { status: make("status"), result: make("result") },
      { status: "loading", result: "**done**" },
    );
    assert.deepEqual(calls, [
      ["status", "loading"],
      ["result", "**done**"],
    ]);
  });

  it("ignores patch keys that have no matching setter", () => {
    const { make, calls } = captureSetters();
    applyPatch(
      { status: make("status") },
      { status: "x", refreshHistory: true },
    );
    assert.deepEqual(calls, [["status", "x"]]);
  });

  it("skips setters whose key is absent from the patch", () => {
    const { make, calls } = captureSetters();
    applyPatch(
      { status: make("status"), error: make("error") },
      { status: "x" },
    );
    assert.deepEqual(calls, [["status", "x"]]);
  });

  it("treats null/undefined patch as a no-op", () => {
    const { make, calls } = captureSetters();
    applyPatch({ status: make("status") }, null);
    applyPatch({ status: make("status") }, undefined);
    assert.deepEqual(calls, []);
  });

  it("forwards falsy values like empty string and false", () => {
    const { make, calls } = captureSetters();
    applyPatch(
      { status: make("status"), loading: make("loading") },
      { status: "", loading: false },
    );
    assert.deepEqual(calls, [
      ["status", ""],
      ["loading", false],
    ]);
  });
});
