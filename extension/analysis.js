// analysis.js — YT Critic v2

const API_BASE = "https://web-production-9c48.up.railway.app";

const SYSTEM_PROMPT = `You must respond exclusively in English.

You are a critical analyst of video content. Read the transcript and produce
a structured critical summary in English with exactly this Markdown structure:

## Topic & Core Argument
(2-3 sentences: what the video is about and its main message)

## Key Points
(maximum 6-8 bullet points, no repetition or padding)

## Fact-Check
For each factual claim, statistic or verifiable data point in the video:
- Use the web_search tool to actually verify it before writing anything
- Classify as: ✅ Confirmed / ❌ False or misleading / ⚠️ Needs further verification
- Briefly cite the source used
If the video is purely opinion or entertainment, state that explicitly.

## Overall Critical Assessment
Is the video reliable? Any obvious bias? Opinions presented as facts?
Be direct and honest, even if the conclusion is negative.

Respond ONLY with the Markdown summary. Never invent content not in the transcript.`;

function el(id) { return document.getElementById(id); }

function showError(msg) {
  el("loader").style.display = "none";
  el("errorBox").textContent = msg;
  el("errorBox").classList.add("visible");
}

function markdownToHtml(md) {
  return md
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/^[-•] (.+)$/gm, "<li>$1</li>")
    .replace(/(<li>.*<\/li>\n?)+/g, m => `<ul>${m}</ul>`)
    .replace(/^(?!<[hlu])(.+)$/gm, "<p>$1</p>")
    .replace(/<p><\/p>/g, "")
    .trim();
}

// ── Transcript extraction ─────────────────────────────────────────────────────

async function getTranscriptFromTab(tabId) {
  let results;
  try {
    results = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: async () => {

        function getTitle() {
          const h1 = document.querySelector("h1.ytd-watch-metadata yt-formatted-string")
                  || document.querySelector("h1.ytd-watch-metadata")
                  || document.querySelector("#title h1");
          const meta = document.querySelector('meta[property="og:title"]');
          return h1?.textContent?.trim()
              || meta?.content
              || document.title.replace(/\s*[-–]\s*YouTube\s*$/, "").trim()
              || "Video";
        }

        function findKey(obj, key, depth = 0) {
          if (!obj || typeof obj !== "object" || depth > 20) return null;
          if (key in obj) return obj[key];
          for (const v of Object.values(obj)) {
            const r = findKey(v, key, depth + 1);
            if (r !== null) return r;
          }
          return null;
        }

        function parseInnertubeSegments(json) {
          const segs = findKey(json, "initialSegments");
          if (Array.isArray(segs) && segs.length) {
            return segs
              .map(s => s?.transcriptSegmentRenderer?.snippet?.runs?.[0]?.text || "")
              .filter(t => t).join(" ").replace(/\s+/g, " ").trim();
          }
          const cues = findKey(json, "cueGroups");
          if (Array.isArray(cues) && cues.length) {
            return cues
              .flatMap(g => g?.transcriptCueGroupRenderer?.cues || [])
              .map(c => c?.transcriptCueRenderer?.cue?.simpleText || "")
              .filter(t => t).join(" ").replace(/\s+/g, " ").trim();
          }
          return "";
        }

        const title = getTitle();
        const cfg   = window.ytcfg?.data_ || {};
        const context = cfg.INNERTUBE_CONTEXT;
        const apiKey  = cfg.INNERTUBE_API_KEY || "";
        const initData = window.ytInitialData;

        // Strategia 1: transcript già in ytInitialData
        if (initData) {
          const panels = initData.engagementPanels || [];
          for (const panel of panels) {
            const r = panel?.engagementPanelSectionListRenderer;
            if (!r) continue;
            const segs = findKey(r?.content, "initialSegments");
            if (Array.isArray(segs) && segs.length) {
              const text = segs
                .map(s => s?.transcriptSegmentRenderer?.snippet?.runs?.[0]?.text || "")
                .filter(t => t).join(" ").replace(/\s+/g, " ").trim();
              if (text.length > 30) return { transcript: text, title, source: "initialData" };
            }
          }
        }

        // Strategia 2: getTranscriptEndpoint in ytInitialData
        if (initData && context) {
          const endpoint = findKey(initData, "getTranscriptEndpoint");
          if (endpoint?.params) {
            const url = `https://www.youtube.com/youtubei/v1/get_transcript?key=${apiKey}`;
            try {
              const resp = await fetch(url, {
                method: "POST",
                credentials: "include",
                headers: {
                  "Content-Type": "application/json",
                  "X-Goog-Visitor-Id": cfg.VISITOR_DATA || "",
                  "X-Youtube-Client-Name": "1",
                  "X-Youtube-Client-Version": context?.client?.clientVersion || "",
                },
                body: JSON.stringify({ context, params: endpoint.params }),
              });
              if (resp.ok) {
                const json = await resp.json();
                const text = parseInnertubeSegments(json);
                if (text.length > 30) return { transcript: text, title, source: "innertube" };
              }
            } catch(_) {}
          }
        }

        // Strategia 3: caption URL da ytInitialPlayerResponse
        const captionTracks = window.ytInitialPlayerResponse
          ?.captions?.playerCaptionsTracklistRenderer?.captionTracks;
        if (captionTracks && captionTracks.length > 0) {
          const track = captionTracks.find(t => t.languageCode === "en")
                     || captionTracks.find(t => !t.kind)
                     || captionTracks[0];
          try {
            const resp = await fetch(track.baseUrl, { credentials: "include" });
            if (resp.ok) {
              const xml = await resp.text();
              if (xml && xml.length > 50) {
                const parser = new DOMParser();
                const doc = parser.parseFromString(xml, "text/xml");
                const nodes = doc.querySelectorAll("text");
                const text = Array.from(nodes)
                  .map(n => n.textContent.trim()).filter(Boolean).join(" ")
                  .replace(/\s+/g, " ").trim();
                if (text.length > 30) return { transcript: text, title, source: "caption-url" };
              }
            }
          } catch(_) {}
        }

        // Strategia 4: apri il pannello trascrizione e leggi i segmenti
        const SEGMENT_SEL = "transcript-segment-view-model, ytd-transcript-segment-renderer";
        const TEXT_SEL    = "span[role='text'], .segment-text, yt-formatted-string";

        const poll = (ms) => new Promise(r => setTimeout(r, ms));

        const extractSegments = () => {
          const segs = document.querySelectorAll(SEGMENT_SEL);
          if (segs.length < 3) return null;
          const text = Array.from(segs)
            .map(s => s.querySelector(TEXT_SEL)?.textContent?.trim() || "")
            .filter(t => t).join(" ").replace(/\s+/g, " ").trim();
          return text.length > 30 ? text : null;
        };

        // Already open?
        let panelText = extractSegments();
        if (panelText) return { transcript: panelText, title, source: "ui-panel" };

        // Try direct transcript button
        const directBtn = document.querySelector(
          'button[aria-label*="transcript" i], button[aria-label*="trascrizione" i], [aria-label*="Open transcript" i]'
        );
        if (directBtn) {
          directBtn.click();
        } else {
          // Open the "..." more actions menu
          const moreBtn = document.querySelector(
            'button[aria-label="More actions"], #button-shape button[aria-label="More actions"], ytd-menu-renderer yt-icon-button button'
          );
          if (moreBtn) {
            moreBtn.click();
            await poll(1200);
            const menuItem = [...document.querySelectorAll(
              'ytd-menu-service-item-renderer, tp-yt-paper-item, yt-list-item-view-model'
            )].find(el => /transcript|trascrizione/i.test(el.textContent));
            if (menuItem) menuItem.click();
          }
        }

        // Wait up to 8s for segments to appear
        for (let i = 0; i < 80; i++) {
          await poll(100);
          panelText = extractSegments();
          if (panelText) return { transcript: panelText, title, source: "ui-panel-click" };
        }

        return {
          error:
            "Could not load transcript.\n\n" +
            "Make sure:\n" +
            "• The video has subtitles/CC enabled\n" +
            "• Press F5 to reload the YouTube page\n" +
            "• The video has finished loading"
        };
      }
    });
  } catch(e) {
    throw new Error("Could not read the YouTube page.\nPress F5 on YouTube and retry.\n\n" + e.message);
  }

  const result = results?.[0]?.result;
  if (!result)      throw new Error("No result from YouTube tab. Press F5 and retry.");
  if (result.error) throw new Error(result.error);
  console.log("[YT Critic] transcript source:", result.source, "| chars:", result.transcript.length);
  return result;
}

// ── Claude API call via Railway backend ──────────────────────────────────────

async function callClaude(title, transcript, userToken, videoUrl) {
  const res = await fetch(`${API_BASE}/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: videoUrl, user_token: userToken, transcript, title }),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Server error" }));
    if (res.status === 429) throw new Error(`Daily limit reached.\n${err.detail}`);
    throw new Error(`Server error ${res.status}: ${err.detail}`);
  }

  const data = await res.json();
  if (!data.analysis) throw new Error("Empty response from server. Please retry.");
  return data;
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function run() {
  const params   = new URLSearchParams(window.location.search);
  const videoUrl = params.get("url");
  const tabId    = parseInt(params.get("tabId"));

  if (!videoUrl || !tabId) {
    showError("Missing parameters. Close this window and click the extension icon again.");
    return;
  }

  el("loaderUrl").textContent = decodeURIComponent(videoUrl);

  let { userToken } = await chrome.storage.local.get("userToken");
  if (!userToken) {
    userToken = crypto.randomUUID();
    await chrome.storage.local.set({ userToken });
  }

  // Step 1: extract transcript
  el("loaderText").textContent = "Reading transcript from YouTube...";
  let transcript, title;
  try {
    ({ transcript, title } = await getTranscriptFromTab(tabId));
  } catch(e) {
    showError(e.message);
    return;
  }

  // Step 2: call Claude via Railway
  el("loaderText").textContent = "Claude is analyzing the video...";
  const warnTimer = setTimeout(() => {
    if (el("loader").style.display !== "none")
      el("loaderText").textContent = "Claude is verifying facts with web search...";
  }, 10000);

  try {
    const data = await callClaude(title, transcript, userToken, videoUrl);
    clearTimeout(warnTimer);

    el("loader").style.display = "none";
    document.title = `YT Critic — ${data.title}`;
    el("resultTitle").textContent = data.title;
    el("resultBody").innerHTML = markdownToHtml(data.analysis);
    el("result").classList.add("visible");
    if (data.remaining_today != null)
      el("quota").textContent = `${data.remaining_today} free analyses left today`;

  } catch(e) {
    clearTimeout(warnTimer);
    showError(e.message || "Could not reach the server.");
  }
}

el("btnCopy").addEventListener("click", async () => {
  await navigator.clipboard.writeText(el("resultBody").innerText);
  el("btnCopy").textContent = "✅ Copied!";
  setTimeout(() => { el("btnCopy").textContent = "📋 Copy text"; }, 2000);
});
el("btnClose").addEventListener("click", () => window.close());

run();
