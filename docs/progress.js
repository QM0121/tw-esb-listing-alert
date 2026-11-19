function eventDateRank(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return 0;

  let m = raw.match(/^(\d{4})(\d{2})(\d{2})$/);
  if (m) return Number(`${m[1]}${m[2]}${m[3]}`);

  m = raw.match(/^(\d{3})(\d{2})(\d{2})$/);
  if (m) return Number(`${Number(m[1]) + 1911}${m[2]}${m[3]}`);

  m = raw.match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/);
  if (m) return Number(`${m[1]}${String(m[2]).padStart(2, "0")}${String(m[3]).padStart(2, "0")}`);

  m = raw.match(/^(\d{2,3})[-/.](\d{1,2})[-/.](\d{1,2})$/);
  if (m) return Number(`${Number(m[1]) + 1911}${String(m[2]).padStart(2, "0")}${String(m[3]).padStart(2, "0")}`);

  return 0;
}

function stageLabel(event) {
  if (event.stage === "掛牌日期已公布") {
    const today = new Intl.DateTimeFormat("sv-SE", {timeZone: "Asia/Taipei"}).format(new Date());
    if (eventDateRank(event.event_date) > eventDateRank(today)) return "預定掛牌";
  }
  return event.stage || "未分類";
}

function dateLabel(event) {
  if (event.stage === "撤回申請" && ["twse_apply", "tpex_apply"].includes(event.source)) {
    return "原申請日";
  }
  return "事件日期";
}

function currentCaseClosed(events, sources) {
  const application = events.find(event => event.stage === "送件");
  const start = application ? eventDateRank(application.event_date) : 0;
  return events.some(event => {
    const date = eventDateRank(event.event_date);
    if (!date || date < start) return false;
    if (event.stage === "撤回申請") return true;
    const verified = sources[event.source]?.last_success_at || "";
    return event.stage === "掛牌日期已公布" && date <= eventDateRank(verified.slice(0, 10));
  });
}
