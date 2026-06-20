// background.js — genera il token utente al primo avvio.
chrome.runtime.onInstalled.addListener(async () => {
  const { userToken } = await chrome.storage.local.get("userToken");
  if (!userToken) {
    await chrome.storage.local.set({ userToken: crypto.randomUUID() });
  }
});
