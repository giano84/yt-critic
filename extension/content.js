// content.js — iniettato su https://*.youtube.com/*
// Risponde a GET_CAPTION_URL con il titolo letto dal DOM.
// La lettura di ytInitialPlayerResponse avviene in analysis.js via
// chrome.scripting.executeScript (MAIN world) — non disponibile qui.

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type !== "GET_TITLE") return false;
  sendResponse({ title: findTitle() });
  return false;
});

function findTitle() {
  const h1 =
    document.querySelector("h1.ytd-watch-metadata yt-formatted-string") ||
    document.querySelector("h1.ytd-watch-metadata") ||
    document.querySelector("#title h1") ||
    document.querySelector("h1.title");
  if (h1?.textContent?.trim()) return h1.textContent.trim();

  const meta = document.querySelector('meta[property="og:title"]');
  if (meta?.content) return meta.content;

  return document.title.replace(/\s*[-–]\s*YouTube\s*$/, "").trim() || "Video";
}
