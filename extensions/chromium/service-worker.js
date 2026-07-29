const ALARM = "dayfinch-active-domain";

async function reportActiveDomain() {
  const {bridgeToken = "", bridgePort = 8765} = await chrome.storage.local.get([
    "bridgeToken",
    "bridgePort"
  ]);
  if (!bridgeToken) return;

  const [tab] = await chrome.tabs.query({active: true, lastFocusedWindow: true});
  let domain = "";
  if (tab && tab.url) {
    const window = await chrome.windows.get(tab.windowId);
    if (window.focused) {
      try {
        const url = new URL(tab.url);
        if (url.protocol === "http:" || url.protocol === "https:") {
          domain = url.hostname.toLowerCase().replace(/^www\./, "");
        }
      } catch (_error) {
        domain = "";
      }
    }
  }

  try {
    await fetch(`http://127.0.0.1:${bridgePort}/v1/active-domain`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token: bridgeToken, domain})
    });
  } catch (_error) {
    // The agent may be paused, stopped, or offline. Browser use is never queued.
  }
}

chrome.runtime.onInstalled.addListener(async ({reason}) => {
  await chrome.alarms.create(ALARM, {periodInMinutes: 0.5});
  if (reason === "install") await chrome.runtime.openOptionsPage();
  await reportActiveDomain();
});
chrome.runtime.onStartup.addListener(reportActiveDomain);
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === ALARM) reportActiveDomain();
});
chrome.tabs.onActivated.addListener(reportActiveDomain);
chrome.tabs.onUpdated.addListener((_tabId, changeInfo, tab) => {
  if (tab.active && changeInfo.url) reportActiveDomain();
});
chrome.windows.onFocusChanged.addListener(reportActiveDomain);
