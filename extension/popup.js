const API_BASE = "https://web-production-9c48.up.railway.app";

function el(id) { return document.getElementById(id); }

function isYouTubeVideo(url) {
  return url && (url.includes("youtube.com/watch") || url.includes("youtu.be/"));
}

async function init() {
  let { userToken } = await chrome.storage.local.get("userToken");
  if (!userToken) {
    userToken = crypto.randomUUID();
    await chrome.storage.local.set({ userToken });
  }

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = tab?.url || "";

  if (isYouTubeVideo(url)) {
    const videoId = new URL(url).searchParams.get("v");
    el("urlText").textContent = videoId
      ? `youtube.com/watch?v=${videoId}`
      : url.replace("https://", "");
    el("btnAnalyze").disabled = false;

    el("btnAnalyze").addEventListener("click", () => {
      const analysisUrl = chrome.runtime.getURL("analysis.html") +
        `?url=${encodeURIComponent(url)}&tabId=${tab.id}`;
      chrome.windows.create({
        url: analysisUrl,
        type: "popup",
        width: 560,
        height: 680,
        focused: true,
      });
      window.close();
    });
  } else {
    el("urlBox").classList.add("no-video");
    el("urlText").textContent = "Open a YouTube video to use YT Critic";
    el("btnAnalyze").disabled = true;
  }

  try {
    const res = await fetch(`${API_BASE}/limit?user_token=${encodeURIComponent(userToken)}`);
    if (res.ok) {
      const data = await res.json();
      el("quotaRemaining").textContent = data.remaining;
    }
  } catch (_) {
    el("quotaRemaining").textContent = "?";
  }
}

init();
