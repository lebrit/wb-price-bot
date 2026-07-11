const form = document.querySelector("#connector-form");
const serverInput = document.querySelector("#server");
const codeInput = document.querySelector("#code");
const button = document.querySelector("#connect");
const statusNode = document.querySelector("#status");

initialize();

async function initialize() {
  const stored = await chrome.storage.local.get(["connectorServer"]);
  if (stored.connectorServer) serverInput.value = stored.connectorServer;
}

function setStatus(text, kind = "") {
  statusNode.textContent = text;
  statusNode.className = kind;
}

function normalizedServer(raw) {
  const url = new URL(raw.trim());
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) {
    throw new Error("Нужен обычный HTTPS-адрес сервера");
  }
  return url.origin;
}

function isWildberries(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host === "wildberries.ru" || host.endsWith(".wildberries.ru");
  } catch {
    return false;
  }
}

function waitForTab(tabId, timeout = 20000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("Wildberries долго загружается. Обновите страницу и повторите."));
    }, timeout);
    function listener(updatedId, info) {
      if (updatedId === tabId && info.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function collectLocalStorage(tabId) {
  const result = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: () => Object.entries(localStorage).map(([name, value]) => ({ name, value })),
  });
  return result[0]?.result || [];
}

function mapSameSite(value) {
  if (value === "strict") return "Strict";
  if (value === "no_restriction") return "None";
  return "Lax";
}

async function collectCookies() {
  const groups = await Promise.all([
    chrome.cookies.getAll({ domain: "wildberries.ru" }),
    chrome.cookies.getAll({ domain: "wb.ru" }),
  ]);
  const unique = new Map();
  for (const cookie of groups.flat()) {
    const key = `${cookie.name}|${cookie.domain}|${cookie.path}`;
    unique.set(key, {
      name: cookie.name,
      value: cookie.value,
      domain: cookie.domain,
      path: cookie.path || "/",
      expires: cookie.expirationDate,
      httpOnly: cookie.httpOnly,
      secure: cookie.secure,
      sameSite: mapSameSite(cookie.sameSite),
    });
  }
  return [...unique.values()];
}

async function currentCapture(tabId) {
  setStatus("Обновляю Wildberries и получаю активную сессию…");
  await chrome.storage.local.remove(["wbConnectorCapture"]);
  await chrome.tabs.reload(tabId);
  await waitForTab(tabId);
  await new Promise((resolve) => setTimeout(resolve, 1200));
  const stored = await chrome.storage.local.get(["wbConnectorCapture"]);
  if (!stored.wbConnectorCapture?.headers?.authorization) {
    throw new Error("Авторизация WB не обнаружена. Войдите в аккаунт и обновите страницу.");
  }
  return stored.wbConnectorCapture;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  button.disabled = true;
  setStatus("Проверяю подключение…");
  try {
    const server = normalizedServer(serverInput.value);
    const code = codeInput.value.trim().toUpperCase();
    if (!/^[A-F0-9]{10}$/.test(code)) throw new Error("Введите код из 10 символов");
    const permission = await chrome.permissions.request({ origins: [`${server}/*`] });
    if (!permission) throw new Error("Разрешите расширению обращаться к вашему серверу");
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || !isWildberries(tab.url || "")) {
      throw new Error("Откройте wildberries.ru, войдите в аккаунт и повторите");
    }
    const connector = await currentCapture(tab.id);
    const cookies = await collectCookies();
    const localStorage = await collectLocalStorage(tab.id);
    if (!cookies.length) {
      throw new Error("Данные аккаунта WB не найдены. Войдите и обновите страницу.");
    }
    setStatus("Передаю зашифрованную сессию серверу…");
    const response = await fetch(`${server}/api/connector`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        code,
        session: {
          cookies,
          origins: [{ origin: new URL(tab.url).origin, localStorage }],
          connector,
        },
      }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.message || `Ошибка сервера ${response.status}`);
    await chrome.storage.local.set({ connectorServer: server });
    await chrome.storage.local.remove(["wbConnectorCapture"]);
    codeInput.value = "";
    setStatus(result.message || "Аккаунт подключён. Вернитесь в Telegram.", "success");
  } catch (error) {
    setStatus(error.message || String(error), "error");
  } finally {
    button.disabled = false;
  }
});
