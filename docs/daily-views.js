(() => {
  const url = "https://tw-esb-visit-count.goldencloud19.chatgpt.site/views";
  const counter = document.getElementById("todayPageviews");
  const dateFormat = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Taipei" });
  let lastRead = 0;
  let displayedDay = "";
  let reading = false;
  let visit;
  let retries = 0;

  function skipCounting() {
    try {
      return navigator.globalPrivacyControl === true || navigator.doNotTrack === "1" ||
        window.localStorage.getItem("skipgc") === "t" || navigator.webdriver ||
        window.top !== window || location.hostname !== "qm0121.github.io";
    } catch {
      return true;
    }
  }

  async function update(method, id) {
    const response = await fetch(url, {
      method, body: id, credentials: "omit", referrerPolicy: "no-referrer",
      cache: "no-store", signal: AbortSignal.timeout(10000)
    });
    if (!response.ok) throw new Error("Counter unavailable");
    const data = await response.json();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(data.day) || !Number.isSafeInteger(data.views) || data.views < 0) {
      throw new Error("Invalid counter response");
    }
    displayedDay = data.day;
    lastRead = Date.now();
    if (counter) counter.textContent = data.views.toLocaleString("zh-TW");
  }

  async function refresh() {
    if (document.visibilityState !== "visible" || reading) return;
    const day = dateFormat.format(new Date());
    if (!visit) visit = { day, id: crypto.randomUUID(), done: skipCounting() };
    if (visit.day !== day) visit.done = true;
    const writing = !visit.done;
    if (!writing && (!counter || (displayedDay === day && Date.now() - lastRead < 55000))) return;
    reading = true;
    try {
      await update(writing ? "POST" : "GET", writing ? visit.id : undefined);
      visit.done = true;
    } catch {
      if (counter) counter.textContent = "暫無資料";
      if (writing && retries++ === 0) window.setTimeout(refresh, 5000);
    } finally {
      reading = false;
    }
  }
  refresh();
  document.addEventListener("visibilitychange", refresh);
  window.addEventListener("online", refresh);
  if (counter) window.setInterval(refresh, 60000);
})();
