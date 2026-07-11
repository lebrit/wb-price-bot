const ALLOWED_HEADERS = new Set([
  "authorization",
  "x-client-version",
  "x-queryid",
  "x-spa-version",
  "x-userid",
]);

chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    const headers = {};
    for (const header of details.requestHeaders || []) {
      const name = String(header.name || "").toLowerCase();
      if (ALLOWED_HEADERS.has(name) && header.value) {
        headers[name] = header.value;
      }
    }
    if (!String(headers.authorization || "").toLowerCase().startsWith("bearer ")) {
      return;
    }
    chrome.storage.local.set({
      wbConnectorCapture: {
        version: 1,
        cardUrl: details.url,
        headers,
        capturedAt: new Date().toISOString(),
      },
    });
  },
  { urls: ["https://card.wb.ru/cards/v4/detail*"] },
  ["requestHeaders", "extraHeaders"],
);
