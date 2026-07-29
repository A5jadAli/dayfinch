const token = document.querySelector("#token");
const port = document.querySelector("#port");
const status = document.querySelector("#status");

chrome.storage.local.get(["bridgeToken", "bridgePort"]).then(settings => {
  token.value = settings.bridgeToken || "";
  port.value = settings.bridgePort || 8765;
});

document.querySelector("#save").addEventListener("click", async () => {
  const bridgeToken = token.value.trim();
  const bridgePort = Number(port.value);
  if (bridgeToken.length < 32 || bridgePort < 1024 || bridgePort > 65535) {
    status.textContent = "Enter a 32+ character token and a valid port.";
    return;
  }
  await chrome.storage.local.set({bridgeToken, bridgePort});
  status.textContent = "Saved.";
});
