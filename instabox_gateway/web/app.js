"use strict";
const {t: translate, join, text: setText} = window.I18n;

// Bindings, drafts and synchronous output live in page memory; auth uses an HttpOnly session.
const $ = (id) => document.getElementById(id);
const el = (tag, text, className) => {
  const n = document.createElement(tag);
  if (text !== undefined) setText(n, text);
  if (className) n.className = className;
  return n;
};
const state = {
  token: "", csrf: "", epoch: 0, accounts: [], inventory: [], requests: [], protections: [],
  settings: null, settingsDirty: false, offline: false, target: null, resume: null,
  executing: false, selecting: false, output: null, channel: "stdout", detail: null,
  sort: "name", direction: 1, refreshing: false, settingsSaving: false
};
const controllers = new Set();
const errors = {
  authentication_required: translate("无法进入管理控制台，请确认使用本地管理 Token。"),
  network_error: translate("无法连接本地 API，请检查本地服务和端口；请求结果可能未知。"),
  invalid_response: translate("本地 API 响应无法识别；请保留原目标，核对请求记录。"),
  duplicate_identity: translate("此身份已接入，请编辑原账号或更换其 Key。"),
  identity_mismatch: translate("新 Key 的身份或组织不匹配，原账号保持不变。"),
  no_eligible_machine: translate("指定账号中没有符合条件的运行实例；未扩大范围或恢复机器。"),
  machine_not_running: translate("目标当前不是运行状态。请查看原实例，不会切换到其他机器。"),
  machine_protected: translate("机器已受保护，仅允许查看控制端元数据。"),
  account_disabled: translate("账号已停用，请由所有者检查本地接入策略。"),
  account_not_found: translate("原账号接入已移除，需显式重新选择目标。"),
  binding_invalid: translate("绑定的凭据版本已失效，请显式重新选择原目标。"),
  credential_changed: translate("凭据版本已变更，原操作无法按当前身份继续。"),
  resume_not_allowed: translate("恢复策略未开启或本次未明确允许恢复。"),
  idempotency_conflict: translate("该恢复记录属于另一目标，未发送新的恢复请求。")
};
class ApiError extends Error {
  constructor(code, outcome, requestId) {
    const safeCode = typeof code === "string" && /^[a-zA-Z0-9_-]{1,96}$/.test(code) ? code : "request_failed";
    const localizedMessage = errors[safeCode] || translate("请求未完成，请检查原目标与访问策略。");
    super(String(localizedMessage)); this.localizedMessage = localizedMessage;
    this.code = safeCode; this.outcome = outcome; this.requestId = requestId;
  }
}
function message(error) {
  if (error.code === "stale_session") return "";
  return error instanceof ApiError ? join(error.localizedMessage, ` [${error.code}]`, error.requestId ? join(" · ", translate("请求 {id}", {id:error.requestId})) : "") : translate("输入或操作无法完成，请检查当前字段。");
}
function feedback(id, text, kind = "") { setText($(id), text); $(id).className = "feedback" + (kind ? " " + kind : ""); }
function notice(text, kind = "") { feedback("notice", text, kind); }
function date(value) {
  if (!value) return translate("尚无成功检查");
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString(window.I18n.locale, {hour12: false}) + translate("（本机时间）");
}
function alias(id) { return state.accounts.find(a => a.id === id)?.alias || id; }
function account(id) { return state.accounts.find(a => a.id === id); }
function badge(text, kind = "") { return el("span", text, "badge" + (kind ? " " + kind : "")); }
function actions(...buttons) { const n = el("div", undefined, "inline-actions"); n.append(...buttons); return n; }
function button(label, action, kind = "secondary", disabled = false) {
  const b = el("button", label, kind); b.type = "button"; b.disabled = disabled;
  b.addEventListener("click", () => busy(b, action)); return b;
}
async function busy(control, action, errorId = "notice") {
  if (control.disabled) return;
  const epoch = state.epoch, old = window.I18n.value(control);
  control.disabled = true; control.setAttribute("aria-busy", "true");
  try { await action(); } catch (error) { if (epoch === state.epoch) feedback(errorId, message(error)); }
  finally { if (epoch === state.epoch) { control.disabled = false; setText(control, old); control.removeAttribute("aria-busy"); } }
}
function connection(ok) {
  state.offline = !ok;
  setText($("connection"), ok ? join(translate("本地 API 可达 · "), new Date().toLocaleTimeString(window.I18n.locale, {hour12: false})) : translate("无法连接本地 API · 当前数据可能过期"));
  $("connection").classList.toggle("offline", !ok);
  updateExecute();
}
async function api(path, method = "GET", body, headers = {}) {
  const epoch = state.epoch, controller = new AbortController(); controllers.add(controller);
  try {
    let response;
    try {
      response = await fetch(path, {method, headers: {...(state.token === "session" ? {"X-Gateway-CSRF":state.csrf} : {Authorization: "Bearer " + state.token}), ...(body === undefined ? {} : {"Content-Type": "application/json"}), ...headers}, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", credentials: "same-origin", signal: controller.signal});
    } catch (_) {
      if (epoch !== state.epoch) throw new ApiError("stale_session");
      connection(false); throw new ApiError("network_error", "UNKNOWN");
    }
    if (epoch !== state.epoch) throw new ApiError("stale_session");
    connection(true);
    let result;
    try { result = await response.json(); } catch (_) { throw new ApiError("invalid_response", "UNKNOWN"); }
    if (epoch !== state.epoch) throw new ApiError("stale_session");
    if (!response.ok) throw new ApiError(result.error?.code, result.error?.outcome, result.error?.requestId);
    return result;
  } finally { controllers.delete(controller); }
}

// Navigation and native dialogs keep browser keyboard / focus behavior.
function closeNav(focus = false) {
  $("sidebar").classList.remove("open"); $("nav-toggle").setAttribute("aria-expanded", "false");
  $("main").inert = false;
  if (focus) $("nav-toggle").focus();
}
function route(focus = true) {
  if (!state.token) return;
  const page = ["instances", "accounts", "execution", "settings"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "instances";
  for (const name of ["instances", "accounts", "execution", "settings"]) $("page-" + name).hidden = name !== page;
  document.querySelectorAll("[data-page]").forEach(n => { if (n.dataset.page === page) n.setAttribute("aria-current", "page"); else n.removeAttribute("aria-current"); });
  closeNav();
  if (focus) $("main").focus({preventScroll: true});
}
function go(page) { if (location.hash === "#" + page) route(); else location.hash = page; }
function openDialog(id) { const d = $(id); if (!d.open) d.showModal(); }
function setDialogBusy(dialog, value) {
  dialog.dataset.busy = String(value); dialog.setAttribute("aria-busy", String(value));
  dialog.querySelectorAll("button,input,select").forEach(n => n.disabled = value);
}
let pendingAction = null;
function confirmAction(title, paragraphs, action, label = translate("确认"), danger = false) {
  if ($("action-dialog").open) return;
  setText($("action-title"), title); $("action-description").replaceChildren(...paragraphs.map(t => el("p", t)));
  feedback("action-error", ""); setText($("action-confirm"), label); $("action-confirm").className = danger ? "danger" : "";
  pendingAction = action; openDialog("action-dialog");
}
$("action-form").addEventListener("submit", async event => {
  event.preventDefault(); const d = $("action-dialog"), epoch = state.epoch;
  if (d.dataset.busy === "true" || !pendingAction) return;
  setDialogBusy(d, true); feedback("action-error", "");
  try { await pendingAction(); if (epoch === state.epoch) d.close(); }
  catch (error) { if (epoch === state.epoch) feedback("action-error", message(error)); }
  finally { if (epoch === state.epoch) setDialogBusy(d, false); }
});
for (const dialog of document.querySelectorAll("dialog")) {
  dialog.addEventListener("cancel", event => { if (dialog.dataset.busy === "true") event.preventDefault(); });
  dialog.querySelectorAll(".close-dialog").forEach(b => b.addEventListener("click", () => dialog.close()));
}
$("account-dialog").addEventListener("close", () => { $("account-form").elements.key.value = ""; });
$("action-dialog").addEventListener("close", () => { pendingAction = null; });

// Independent metadata reads: one failed account/section never clears the rest.
async function loadAccounts() {
  try {
    const data = await api("/api/admin/accounts"); state.accounts = data.accounts; feedback("accounts-error", "");
    const filter = $("account-filter"), previous = filter.value;
    filter.replaceChildren(new Option(translate("全部账号"), ""), ...state.accounts.map(a => new Option(a.alias + " · " + a.id.slice(-6), a.id)));
    filter.value = state.accounts.some(a => a.id === previous) ? previous : "";
    renderAccounts(); renderInventory(); renderTarget(); renderRequests();
  } catch (error) { if (error.code !== "stale_session") feedback("accounts-error", join(message(error), translate(" 已显示的数据可能过期。"))); }
}
let inventoryRead = 0;
async function loadInventory(accountId = "") {
  const read = ++inventoryRead;
  try {
    const data = await api("/api/v1/machines" + (accountId ? "?accountId=" + encodeURIComponent(accountId) : ""));
    if (read !== inventoryRead) return;
    state.inventory = accountId ? [...state.inventory.filter(r => r.accountId !== accountId), ...data.accounts] : data.accounts;
    feedback("inventory-error", ""); renderInventory(); renderAccounts(); renderTarget();
    if (state.detail && $("machine-dialog").open) {
      const fresh = resources().find(x => x.row.accountId === state.detail.row.accountId && x.machine.id === state.detail.machine.id);
      if (fresh) showMachine(fresh);
    }
  } catch (error) {
    if (error.code === "stale_session" || read !== inventoryRead) return;
    state.inventory = state.inventory.map(r => ({...r, complete: false, cached: r.machines.length > 0, error: {code: "local_read_failed"}}));
    feedback("inventory-error", join(message(error), translate(" 保留上次数据，仅供查看。"))); renderInventory(); renderTarget();
  }
}
async function loadRequests() {
  try { state.requests = (await api("/api/admin/requests")).requests; feedback("requests-error", ""); renderRequests(); }
  catch (error) { if (error.code !== "stale_session") feedback("requests-error", join(message(error), translate(" 已显示记录可能过期。"))); }
}
async function loadSettings() {
  try {
    state.settings = await api("/api/admin/settings"); feedback("settings-error", "");
    if (!state.settingsDirty && !state.settingsSaving) {
      const f = $("settings-form"); f.elements.fileRoot.value = state.settings.fileRoot; f.elements.allowResume.checked = state.settings.allowResume;
    }
    renderTarget();
  } catch (error) { if (error.code !== "stale_session") feedback("settings-error", message(error)); }
}
async function loadProtections() {
  try {
    state.protections = (await api("/api/admin/protections")).machines; feedback("protections-error", "");
    const ids = new Set(state.protections.map(p => p.id));
    state.inventory.forEach(r => r.machines.forEach(m => { m.protected = ids.has(m.id); }));
    renderProtections(); renderInventory(); renderTarget();
  } catch (error) { if (error.code !== "stale_session") feedback("protections-error", message(error)); }
}
async function refresh() {
  if (state.refreshing) return; state.refreshing = true;
  const epoch = state.epoch;
  try { await Promise.allSettled([loadAccounts(), loadInventory(), loadRequests(), loadSettings(), loadProtections()]); }
  finally { if (epoch === state.epoch) state.refreshing = false; }
}
function resources() { return state.inventory.flatMap(row => row.machines.map(machine => ({row, machine}))); }
function source(row) { return (row.complete ? translate("最新读取") : row.cached ? translate("缓存，仅供查看") : translate("无可用数据")) + " · " + date(row.checkedAt); }
function stateLabel(m, row) { return (!row.complete ? translate("上次观察：") : "") + ({running: translate("运行"), suspended: translate("暂停")}[m.state] || translate("其他：") + m.state); }
function restriction({machine: m, row}) {
  if (m.protected || state.protections.some(p => p.id === m.id)) return translate("已保护 · 仅查看元数据");
  if (!account(row.accountId)?.enabled) return translate("账号已停用或尚未读取");
  if (!row.complete) return translate("需重新完整读取账号资源");
  return "";
}
function empty(title, description, action) {
  const n = el("div", undefined, "empty"); n.append(el("h2", title), el("p", description)); if (action) n.append(action); return n;
}
function appendCell(row, value) { const td = el("td"); td.append(value instanceof Node ? value : el("span", value)); row.append(td); return td; }
function nameBlock(name, id) { const n = el("div"); n.append(el("strong", name), el("small", id, "mono")); return n; }
function statusBlock(item) {
  const {machine: m, row} = item, n = el("div");
  n.append(badge(stateLabel(m, row), row.complete && m.state === "running" ? "success" : ""));
  const why = restriction(item); if (why) n.append(el("small", why)); return n;
}
function renderInventory() {
  const complete = state.inventory.filter(r => r.complete), current = complete.flatMap(r => r.machines), cached = state.inventory.filter(r => !r.complete).flatMap(r => r.machines);
  const summary = el("div", undefined, "summary-title"); summary.append(el("strong", translate("{complete} / {total} 个接入账号完整读取", {complete:complete.length,total:state.accounts.length})), el("small", state.inventory.length ? translate("数量基于本次完整读取资源；缓存单列，不代表当前可用。") : translate("尚未取得资源列表")));
  $("inventory-summary").replaceChildren(summary, badge(translate("{count} 运行", {count:current.filter(m => m.state === "running").length}), "success"), badge(translate("{count} 暂停", {count:current.filter(m => m.state === "suspended").length})), badge(translate("{count} 已保护", {count:resources().filter(x => x.machine.protected).length})), badge(translate("{count} 缓存", {count:cached.length}), cached.length ? "warning" : ""));
  $("inventory-errors").replaceChildren();
  for (const r of state.inventory.filter(r => !r.complete)) {
    const n = el("div", undefined, "info-box warning"); n.append(el("strong", translate("{account} · 资源读取未完成", {account:alias(r.accountId)})), el("p", (r.error?.code || "unknown") + " · " + source(r)), button(translate("重新读取此账号"), () => loadInventory(r.accountId))); $("inventory-errors").append(n);
  }
  const query = $("machine-search").value.toLocaleLowerCase(), aid = $("account-filter").value, filter = $("state-filter").value, protection = $("protection-filter").value;
  const rows = resources().filter(({row, machine: m}) => (!aid || aid === row.accountId) && (!filter || (filter === "other" ? !["running", "suspended"].includes(m.state) : m.state === filter)) && (!protection || (protection === "protected") === !!m.protected) && (!query || (m.name + " " + m.id).toLocaleLowerCase().includes(query)));
  rows.sort((a,b) => state.direction * (state.sort === "name" ? (a.machine.name || a.machine.id).localeCompare(b.machine.name || b.machine.id) : alias(a.row.accountId).localeCompare(alias(b.row.accountId))));
  if (!rows.length) {
    const title = !state.accounts.length ? translate("尚无账号接入") : translate("没有符合条件的实例");
    const description = state.inventory.some(r => !r.complete) ? translate("部分账号读取失败；没有显示的资源不等于账号无机器。") : translate("尝试调整筛选；有效账号可以暂时没有机器。");
    $("inventory").replaceChildren(empty(title, description, !state.accounts.length ? button(translate("添加账号"), () => { go("accounts"); openAccount(); }) : undefined)); return;
  }
  const wrap = el("div", undefined, "table-wrap resource-desktop"), table = el("table"), head = el("thead"), hr = el("tr"), body = el("tbody"), mobile = el("div", undefined, "resource-mobile");
  for (const [key,label] of [["name",translate("实例")],["account",translate("账号")],["",translate("状态 / 限制")],["",translate("数据来源")],["",translate("操作")]]) {
    const th = el("th"); th.scope = "col";
    if (key) {
      th.setAttribute("aria-sort", state.sort === key ? state.direction === 1 ? "ascending" : "descending" : "none");
      const sort = button(label + (state.sort === key ? state.direction === 1 ? " ↑" : " ↓" : ""), () => {
        state.direction = state.sort === key ? -state.direction : 1; state.sort = key; renderInventory();
        document.querySelector('[data-sort="' + key + '"]')?.focus();
      }, "ghost");
      sort.dataset.sort = key; th.append(sort);
    } else setText(th, label);
    hr.append(th);
  }
  head.append(hr);
  for (const item of rows) {
    const {row, machine: m} = item, tr = el("tr");
    appendCell(tr, nameBlock(m.name || m.id, m.id)); appendCell(tr, nameBlock(alias(row.accountId), row.accountId)); appendCell(tr, statusBlock(item)); appendCell(tr, source(row)); appendCell(tr, button(translate("查看详情"), () => showMachine(item))); body.append(tr);
    const card = el("article", undefined, "machine-card"); card.append(el("small", alias(row.accountId)), el("h3", m.name || m.id), el("code", m.id), statusBlock(item), el("p", source(row), "hint"), button(translate("查看详情"), () => showMachine(item))); mobile.append(card);
  }
  table.append(head,body); wrap.append(table); $("inventory").replaceChildren(wrap,mobile);
}
function pair(label, value, code = false) {
  const n = el("div", undefined, "data-pair"); n.append(el("dt", label), el("dd", value, code ? "code" : "")); return n;
}
function showMachine(item) {
  state.detail = item; const {row, machine: m} = item, a = account(row.accountId), why = restriction(item), list = el("dl", undefined, "detail-list");
  list.append(pair(translate("机器"), m.name || m.id), pair(translate("机器 ID"), m.id, true), pair(translate("账号 / 接入 ID"), alias(row.accountId) + " / " + row.accountId), pair(translate("状态"), stateLabel(m,row)), pair(translate("数据来源"), source(row)), pair(translate("组织上下文"), m.orgId || a?.orgId || translate("个人默认上下文")), pair(translate("访问"), why || translate("未受保护；执行前由服务重新校验")));
  if (m.operationId) list.append(pair(translate("上游 operationId"), m.operationId, true));
  const block = el("div", undefined, "inline-actions");
  block.append(button(translate("重新读取账号"), () => loadInventory(row.accountId)));
  if (!why && m.state === "running") block.append(button(translate("固定此目标"), () => { $("machine-dialog").close(); changeTarget(() => bind({target: {accountId: row.accountId, machineId: m.id}})); }, "", state.executing || state.selecting));
  else if (!why && m.state === "suspended") block.append(button(translate("恢复此实例…"), () => { $("machine-dialog").close(); beginResume(item); }, "", !state.settings?.allowResume || !!state.resume && !["succeeded","failed","FAILED"].includes(state.resume.record.state)));
  if (!m.protected) block.append(button(translate("加入保护清单"), async () => { await protect(m.id, true); $("machine-dialog").close(); }));
  $("machine-detail").replaceChildren(list, el("p", why || (m.state === "suspended" && !state.settings?.allowResume ? translate("恢复策略关闭。请在设置中显式开启，再单独确认本次恢复。") : translate("共享机器，无独占保证。固定目标后不会因筛选或优先级变更迁移。")), "hint"), block); openDialog("machine-dialog");
}

// Account forms never read a stored upstream key back into the UI.
let accountMode = "add", editingAccount = null;
function openAccount(a, mode = "add") {
  accountMode = mode; editingAccount = a || null;
  const f = $("account-form"); f.reset(); f.elements.alias.value = a?.alias || ""; f.elements.priority.value = a?.priority ?? 100; f.elements.key.value = "";
  $("key-field").hidden = mode === "edit"; f.elements.key.required = mode !== "edit";
  setText($("account-dialog-title"), {add:translate("添加账号"),edit:translate("编辑账号信息"),key:translate("更换账号 Key")}[mode]);
  setText($("account-impact"), mode === "key" ? translate("须匹配原身份与组织。即使输入同一 Key，也会更新凭据版本，使旧绑定失效；不会终止已接受的请求。") : mode === "edit" ? translate("别名与优先级只改变本地配置，优先级不影响已有固定目标。") : translate("仅校验身份与完整机器列表；空账号有效，不代表所有执行权限均已验证。"));
  setText($("save-account"), mode === "edit" ? translate("保存信息") : translate("校验并保存")); feedback("account-error", ""); openDialog("account-dialog");
}
function renderAccounts() {
  const nodes = state.accounts.map(a => {
    const n = el("article", undefined, "panel"), header = el("div", undefined, "account-header"), title = nameBlock(a.alias, a.id), meta = el("div", undefined, "account-meta"), inventory = state.inventory.find(r => r.accountId === a.id);
    header.append(title, badge(a.enabled ? translate("本地已启用") : translate("本地已停用"), a.enabled ? "success" : ""));
    for (const [label,value] of [[translate("身份 / 组织"),a.userId + " / " + (a.orgId || translate("个人默认上下文"))],[translate("优先级"),String(a.priority)],[translate("权限范围"),a.scopes === null || a.scopes === undefined ? translate("未报告权限范围") : a.scopes.join(", ") || translate("空权限范围")],[translate("接入校验"),date(a.checkedAt)],[translate("最近资源读取"),inventory ? source(inventory) : translate("尚未读取")],[translate("机器数量"),inventory?.complete ? String(inventory.machines.length) : inventory?.cached ? inventory.machines.length + translate("（缓存）") : translate("未知")]]) {
      const part = el("div"); part.append(el("strong",label),el("span",value)); meta.append(part);
    }
    const controls = el("div", undefined, "account-actions"); controls.append(button(translate("编辑信息"), () => openAccount(a,"edit")), button(a.enabled ? translate("停用接入") : translate("启用接入"), () => confirmAction(a.enabled ? translate("停用本地接入") : translate("启用本地接入"), [a.alias + " · " + a.id, translate("仅改变本地访问策略，不控制云端机器生命周期，也不保证取消已接受请求。")], async () => { await api("/api/admin/accounts/"+a.id,"PATCH",{enabled:!a.enabled}); await loadAccounts(); notice(translate("本地接入策略已更新。"),"success"); })), button(translate("更换 Key"), () => openAccount(a,"key")), button(translate("移除接入"), () => confirmAction(translate("从本地移除账号接入"), [a.alias + " · " + a.id,translate("不会删除云端账号或机器。旧绑定失效，重新接入也不会恢复旧绑定。")], async () => { await api("/api/admin/accounts/"+a.id,"DELETE"); await loadAccounts(); await loadInventory(); notice(translate("已移除本地接入，云端机器保留。"),"success"); },translate("移除接入"),true),"danger"));
    n.append(header,meta,controls); return n;
  });
  $("accounts").replaceChildren(...(nodes.length ? nodes : [empty(translate("尚无账号接入"),translate("添加 InstaBox API Key 后，先校验身份与完整机器列表。"),button(translate("添加账号"),()=>openAccount()))]));
}
$("account-form").addEventListener("submit", async event => {
  event.preventDefault(); const dialog = $("account-dialog"), f = event.target, epoch = state.epoch;
  if (dialog.dataset.busy === "true") return;
  const body = {alias:f.elements.alias.value,priority:Number(f.elements.priority.value)};
  if (accountMode !== "edit") body.apiKey = f.elements.key.value;
  const method = accountMode === "add" ? "POST" : accountMode === "edit" ? "PATCH" : "PUT";
  const path = "/api/admin/accounts" + (editingAccount ? "/" + editingAccount.id + (accountMode === "key" ? "/key" : "") : "");
  f.elements.key.value = ""; setDialogBusy(dialog,true); feedback("account-error",accountMode === "edit" ? translate("正在保存…") : translate("正在校验身份与完整机器列表…"),"warning");
  try { await api(path,method,body); if (epoch !== state.epoch) return; dialog.close(); await loadAccounts(); await loadInventory(); notice(translate("账号信息已保存。身份及列表校验不代表拥有全部执行权限。"),"success"); }
  catch(error) { if (epoch === state.epoch) feedback("account-error",message(error)); }
  finally { if (epoch === state.epoch) setDialogBusy(dialog,false); }
});

// Fixed targets. View filters never enter the execution payload.
function changeTarget(action) {
  if (state.executing || state.selecting) return;
  const draft = ["command","exec-cwd","exec-stdin","exec-envs"].some(id=>$(id).value);
  if (state.target || draft) confirmAction(translate("显式更换执行目标"), [translate("现有结果保留原目标标签。将为新目标建立绑定，不会迁移已提交的请求。"), translate("当前未提交草稿会保留，请确认它适用于新目标。")],action,translate("保留草稿并选择"));
  else return action();
}
async function bind(body) {
  if (state.selecting || state.executing) return;
  const epoch = state.epoch;
  state.selecting = true; renderTarget();
  try {
    const data = await api("/api/v1/bindings","POST",body);
    state.target = {...data.binding,machine:data.machine};
    renderTarget(); go("execution"); setExecutionTab(false); notice(translate("已固定目标。之后的账号筛选不会改变此绑定。"),"success");
  } finally { if (epoch === state.epoch) { state.selecting = false; if (state.token) renderTarget(); } }
}
function targetBlocked() {
  if (!state.target) return translate("尚未选择目标");
  const t = state.target, a = account(t.accountId), item = resources().find(x=>x.row.accountId === t.accountId && x.machine.id === t.machineId);
  if (!a?.enabled) return translate("原账号未启用或已移除");
  if (a.version !== t.version) return translate("凭据版本已变更，需显式重新选择");
  if (!item && state.inventory.some(r => r.accountId === t.accountId && r.complete)) return translate("原目标不在最新列表中，请重新检查");
  if (state.protections.some(p=>p.id === t.machineId) || item?.machine.protected) return translate("目标已受保护");
  if (item && !item.row.complete) return translate("原账号资源需重新检查");
  if ((item?.machine.state || t.machine.state) !== "running") return translate("原目标当前不是运行状态");
  return "";
}
function updateExecute() {
  $("execute").disabled = !state.token || state.offline || state.executing || state.selecting || !!targetBlocked();
  setText($("execute"), state.executing ? translate("请求进行中，不重复提交…") : translate("在固定目标执行"));
}
function renderTarget() {
  const container = $("target");
  if (!state.target) {
    const text = el("div",undefined,"target-info"); text.append(el("h2",translate("尚未选择执行目标")),el("p",translate("选择会形成固定绑定；账号查看范围不会改变它。"),"hint"));
    container.replaceChildren(text,actions(button(translate("去实例页选择"),()=>go("instances")),button(translate("自动选择运行实例"),openAuto,"",state.selecting || state.executing))); updateExecute(); return;
  }
  const t = state.target, text = el("div",undefined,"target-info"), reason = targetBlocked();
  text.append(el("p","FIXED TARGET","eyebrow"),el("h2",alias(t.accountId)+" / "+(t.machine.name || t.machineId)),el("code",t.accountId+" / "+t.machineId),el("small",translate("绑定 ")+t.id+" · "+(t.reason === "explicit" ? translate("显式选择") : translate("账号优先级与稳定顺序"))),el("small",reason || translate("共享机器，无独占保证；执行前由服务再次校验。")));
  container.replaceChildren(text,actions(button(translate("更换目标"),()=>{ go("instances"); notice(translate("请选择新实例；现有固定目标保持不变。")); },"secondary",state.executing || state.selecting),button(translate("自动选择…"),()=>changeTarget(openAuto),"secondary",state.executing || state.selecting))); updateExecute();
}
function openAuto() {
  const list = $("auto-accounts"); list.replaceChildren(el("legend",translate("候选账号")));
  for (const a of state.accounts) {
    const label = el("label",undefined,"check"), input = el("input"); input.type="checkbox"; input.value=a.id; input.checked=a.enabled; input.disabled=!a.enabled;
    label.append(input,el("span",a.alias+" · "+a.id+translate(" · 优先级 ")+a.priority+(a.enabled ? "" : translate("（已停用）")))); list.append(label);
  }
  feedback("auto-error",""); openDialog("auto-dialog");
}
$("auto-form").addEventListener("submit",async event=>{
  event.preventDefault(); const d=$("auto-dialog"), epoch=state.epoch;
  if(d.dataset.busy === "true") return;
  const accountIds=[...d.querySelectorAll("input:checked:not(:disabled)")].map(n=>n.value);
  if(!accountIds.length){feedback("auto-error",translate("请至少选择一个启用的候选账号。"));return;}
  setDialogBusy(d,true);
  try{await bind({accountIds,allowWake:false});if(epoch===state.epoch)d.close();}
  catch(error){if(epoch===state.epoch)feedback("auto-error",message(error));}
  finally{if(epoch===state.epoch){setDialogBusy(d,false);d.querySelectorAll("input").forEach(n=>n.disabled=!account(n.value)?.enabled);}}
});

// Synchronous execution, with response-only output and no automatic retry.
const outcomes = {SUCCEEDED:[translate("已完成"),"success"],NONZERO:[translate("命令返回非零"),"danger"],TIMED_OUT:[translate("执行超时"),"warning"],FAILED:[translate("请求失败"),"danger"],UNKNOWN:[translate("结果未知"),"warning"],ACCEPTED:[translate("已接受，结果未结算"),"info"],PENDING:[translate("等待原操作结果"),"info"]};
function outcomeBadge(value) { return badge(outcomes[value]?.[0] || translate("未识别结果：")+value,outcomes[value]?.[1] || "warning"); }
function setExecutionTab(history) {
  $("history").hidden=!history; $("workbench").hidden=history;
  for(const [id,active] of [["history-tab",history],["workbench-tab",!history]]) {$(id).classList.toggle("active",active);$(id).setAttribute("aria-pressed",String(active));}
  if(history) loadRequests();
}
function commandBody() {
  const body={target:{bindingId:state.target.id},timeoutSeconds:Number($("exec-timeout").value)}, text=$("command").value;
  const size=s=>new TextEncoder().encode(s).length;
  if($("command-mode").value === "argv") {
    let argv;try{argv=JSON.parse(text);}catch(_){throw new ApiError("invalid_argv");}
    if(!Array.isArray(argv)||!argv.length||argv.length>4096||argv.some(a=>typeof a!=="string")||!argv[0]||argv.reduce((n,a)=>n+size(a)+1,0)>65536)throw new ApiError("invalid_argv");
    body.argv=argv;
  } else {if(!text||size(text)>65536)throw new ApiError("invalid_command");body.cmd=text;}
  if(!Number.isInteger(body.timeoutSeconds)||body.timeoutSeconds<1||body.timeoutSeconds>120)throw new ApiError("invalid_timeout");
  if($("exec-cwd").value)body.cwd=$("exec-cwd").value;
  if($("exec-stdin").value){if(size($("exec-stdin").value)>1048576)throw new ApiError("stdin_too_large");body.stdin=$("exec-stdin").value;}
  if($("exec-envs").value){
    let envs;try{envs=JSON.parse($("exec-envs").value);}catch(_){throw new ApiError("invalid_envs");}
    if(!envs||Array.isArray(envs)||typeof envs!=="object"||Object.keys(envs).length>256||Object.entries(envs).some(([k,v])=>!/^[A-Za-z_][A-Za-z0-9_]*$/.test(k)||typeof v!=="string"||size(v)>65536))throw new ApiError("invalid_envs");body.envs=envs;
  }
  if(size(JSON.stringify(body))>2*1024*1024)throw new ApiError("request_too_large");
  return body;
}
Object.assign(errors,{invalid_argv:translate("argv 需为非空 JSON 字符串数组，合计最多 64KiB。"),invalid_command:translate("命令不能为空且不得超过 64KiB。"),invalid_timeout:translate("超时需为 1–120 秒整数。"),stdin_too_large:translate("stdin 不得超过 1MiB。"),invalid_envs:translate("环境变量需为字符串值 JSON 对象，最多 256 项，键名与值长度需符合约束。"),request_too_large:translate("请求 JSON 不得超过 2MiB。")});
$("exec-form").addEventListener("submit",async event=>{
  event.preventDefault();if(state.executing || state.selecting || targetBlocked() || state.offline)return;
  let body;try{body=commandBody();}catch(error){feedback("exec-error",message(error));return;}
  const epoch=state.epoch, target={...state.target},started=performance.now();
  state.executing=true;state.output=null;state.channel="stdout";feedback("exec-error","");feedback("result-warning","");
  $("result-badge").replaceWith(Object.assign(outcomeBadge("ACCEPTED"),{id:"result-badge"}));
  $("result-meta").replaceChildren(el("p",alias(target.accountId)+" / "+target.machineId));setText($("output"),translate("等待完整响应，不提供实时日志…"));$("copy-output").disabled=true;updateExecute();renderTarget();
  try{
    const data=await api("/api/v1/exec","POST",body);
    state.output={...data,elapsed:((performance.now()-started)/1000).toFixed(1),target};
  }catch(error){
    if(epoch!==state.epoch)return;
    state.output={outcome:error.outcome === "FAILED" ? "FAILED":"UNKNOWN",requestId:error.requestId,target,error:message(error)};
    feedback("exec-error",message(error));
  }finally{
    if(epoch===state.epoch){state.executing=false;renderResult();renderTarget();loadRequests();}
  }
});
function renderResult() {
  const data=state.output;if(!data)return;
  const b=outcomeBadge(data.outcome);b.id="result-badge";$("result-badge").replaceWith(b);
  const result=data.result, meta=[el("p",alias(data.target.accountId)+" / "+data.target.machineId,"mono"),el("p",translate("请求 ID：")+(data.requestId || translate("响应未提供；请人工核对最近记录")),"mono")];
  if(result)meta.push(el("p",`exitCode ${result.exitCode} · timedOut ${result.timedOut} · truncated ${result.truncated}`,"mono"),el("p",translate("响应耗时 {seconds} 秒（含网络等待）", {seconds:data.elapsed}),"muted"));
  $("result-meta").replaceChildren(...meta);
  const warnings=[];
  if(data.outcome === "UNKNOWN")warnings.push(translate("结果未知。勿盲目重复提交，不会自动换机。请保留原目标并人工核对请求记录及远端状态。"));
  if(result?.timedOut)warnings.push(translate("超时不保证后台进程或容器已终止。请先核对原目标，勿直接重跑。"));
  if(result?.truncated)warnings.push(translate("输出已截断，当前不是完整输出。"));
  if(data.outcome === "FAILED")warnings.push(translate("请求失败不代表远端进程已被清理。"));
  feedback("result-warning",join(...warnings.flatMap((w,i)=>i ? ["\n",w]:[w])),"warning");renderOutput();
}
function renderOutput(){
  const result=state.output?.result, text=result?.[state.channel];
  setText($("output"),result ? (text || translate("{channel} 为空",{channel:state.channel})) : state.output ? translate("未取得输出；历史仅有请求元数据。") : translate("尚无输出"));
  $("stdout-tab").classList.toggle("active",state.channel === "stdout");$("stderr-tab").classList.toggle("active",state.channel === "stderr");
  $("stdout-tab").setAttribute("aria-pressed",String(state.channel === "stdout"));$("stderr-tab").setAttribute("aria-pressed",String(state.channel === "stderr"));
  $("copy-output").disabled=!text;
  const query=$("output-search").value;
  setText($("output-matches"),query && result ? translate("当前通道匹配 {count} 处",{count:text.toLocaleLowerCase().split(query.toLocaleLowerCase()).length-1}) : "");
}
function renderRequests(){
  const query=$("request-search").value.toLocaleLowerCase(),rows=state.requests.filter(r=>[r.id,r.action,r.accountId,alias(r.accountId),r.machineId,r.outcome,r.error].join(" ").toLocaleLowerCase().includes(query));
  if(!rows.length){$("requests").replaceChildren(empty(translate("暂无匹配的请求记录"),translate("仅筛选最近 100 条已接受请求；校验阶段拒绝的操作可能没有请求记录。")));return;}
  const wrap=el("div",undefined,"table-wrap history-desktop"),table=el("table"),head=el("thead"),tr=el("tr"),body=el("tbody"),mobile=el("div");
  for(const text of [translate("接受时间"),translate("动作 / 请求 ID"),translate("账号 / 机器"),translate("结果 / 元数据")]){const th=el("th",text);th.scope="col";tr.append(th);}head.append(tr);
  for(const r of rows){
    const row=el("tr"),details=el("div");details.append(outcomeBadge(r.outcome));
    for(const [k,v]of Object.entries(r).filter(([k])=>["exitCode","timedOut","truncated","error","operationId","finishedAt"].includes(k)))details.append(el("small",k+": "+(k==="finishedAt" ? date(v):v),"mono"));
    appendCell(row,date(r.acceptedAt));appendCell(row,nameBlock(r.action,r.id));appendCell(row,nameBlock(alias(r.accountId),r.accountId+" / "+r.machineId));appendCell(row,details);body.append(row);
    const card=el("article",undefined,"panel request-card");card.append(el("small",date(r.acceptedAt)),el("h3",r.action+" · "+alias(r.accountId)),el("p",r.machineId,"mono"),details.cloneNode(true),el("small",r.id,"mono"));mobile.append(card);
  }
  table.append(head,body);wrap.append(table);$("requests").replaceChildren(wrap,mobile);
}

// Resume follows exactly one explicit target and its original operation.
function beginResume(item){
  if(!state.settings?.allowResume){notice(translate("恢复策略未开启。请在设置中检查，开启策略本身不会恢复机器。"));return;}
  const {row,machine:m}=item;
  confirmAction(translate("显式恢复这台暂停实例"),[alias(row.accountId)+" / "+m.id,translate("本次允许恢复，可能增加运行时间。禁用 Vault 投递。恢复后保留原目标，不会自动执行命令。")],async()=>{
    const id="ui-"+crypto.randomUUID();
    // Keep the intent even when the browser loses the response. Never generate
    // another key automatically or submit the same action to another machine.
    state.resume={record:{id,state:"SENDING",operationId:null},accountId:row.accountId,machineId:m.id};renderResume();
    try{state.resume.record=await api("/api/v1/resumes","POST",{target:{accountId:row.accountId,machineId:m.id},allowWake:true},{"Idempotency-Key":id});}
    catch(error){if(error.code === "stale_session")throw error;state.resume.record.state=error.outcome === "FAILED" ? "FAILED":"UNKNOWN";state.resume.error=message(error);}
    renderResume();go("execution");setExecutionTab(false);loadRequests();
  },translate("允许本次恢复"));
}
function renderResume(){
  const entry=state.resume;$("resume-panel").hidden=!entry;if(!entry)return;
  const r=entry.record,status={SENDING:translate("请求发送中"),queued:translate("原操作排队中"),running:translate("原操作进行中"),succeeded:translate("原操作已成功"),failed:translate("原操作失败"),FAILED:translate("请求失败"),UNKNOWN:translate("结果未知")}[r.state] || r.state;
  const list=el("div",undefined,"stack");list.append(el("p",alias(entry.accountId)+" / "+entry.machineId),el("p",status),el("code",translate("本地记录 ID：")+r.id));
  if(r.operationId)list.append(el("code",translate("上游 operationId：")+r.operationId));
  if(entry.error)list.append(el("p",entry.error,"feedback"));
  if(r.state === "succeeded")list.append(button(translate("重新检查并固定原目标"),async()=>{await loadInventory(entry.accountId);await changeTarget(()=>bind({target:{accountId:entry.accountId,machineId:entry.machineId}}));}));
  if(r.state === "UNKNOWN")list.append(el("p",translate("不要新建恢复请求。保留该 ID，先查询原记录或人工对账。"),"hint"));
  $("resume-status").replaceChildren(list);$("check-resume").disabled=r.state === "SENDING";
}
$("check-resume").addEventListener("click",()=>busy($("check-resume"),async()=>{
  if(!state.resume)return;
  state.resume.record=await api("/api/v1/resumes/"+encodeURIComponent(state.resume.record.id));state.resume.error="";renderResume();loadRequests();
}));
async function protect(id,protectedValue){
  await api("/api/admin/protections","PUT",{machineId:id,protected:protectedValue});await loadProtections();notice(protectedValue ? translate("保护已生效；不保证终止已接受的请求。"):translate("已解除本地保护，未执行或恢复机器。"),"success");
}
function renderProtections(){
  $("protections").replaceChildren(...state.protections.map(p=>{
    const n=el("div",undefined,"protection-item");n.append(el("code",p.id),button(translate("解除保护…"),()=>confirmAction(translate("解除机器保护"),[p.id,translate("解除后符合策略的请求可访问该机器；此操作不会发起执行或恢复。")],()=>protect(p.id,false),translate("解除保护"),true),"danger"));return n;
  }));if(!state.protections.length)$("protections").append(el("p",translate("保护清单为空，可从实例详情添加。"),"hint"));
}
$("protect-form").addEventListener("submit",event=>{
  event.preventDefault();const f=event.target;busy(f.querySelector("button"),async()=>{await protect(f.elements.machineId.value,true);f.reset();feedback("protect-error","");},"protect-error");
});
$("settings-form").addEventListener("input",()=>{state.settingsDirty=true;feedback("settings-feedback",translate("有未保存的策略更改。"),"warning");});
$("settings-form").addEventListener("submit",event=>{
  event.preventDefault();const f=event.target,epoch=state.epoch,body={fileRoot:f.elements.fileRoot.value,allowResume:f.elements.allowResume.checked};
  busy(f.querySelector("button"),async()=>{
    state.settingsSaving=true;
    f.querySelectorAll("input").forEach(n=>n.disabled=true);
    try{state.settings=await api("/api/admin/settings","PATCH",body);state.settingsDirty=false;feedback("settings-feedback",translate("策略已保存；没有恢复任何机器。"),"success");renderTarget();}
    finally{if(epoch===state.epoch){state.settingsSaving=false;f.querySelectorAll("input").forEach(n=>n.disabled=false);}}
  },"settings-feedback");
});

// Session teardown invalidates in-flight completions before clearing the UI.
function logout(){
  state.epoch++;state.token="";state.csrf="";inventoryRead++;controllers.forEach(c=>c.abort());controllers.clear();
  window.dispatchEvent(new Event("igw:logout"));
  Object.assign(state,{accounts:[],inventory:[],requests:[],protections:[],settings:null,settingsDirty:false,settingsSaving:false,target:null,resume:null,output:null,detail:null,executing:false,selecting:false,refreshing:false,offline:false,channel:"stdout"});
  for(const d of document.querySelectorAll("dialog")){if(d.open)d.close();setDialogBusy(d,false);}
  // Old async handlers deliberately skip updates after an epoch change. Reset
  // their controls here so a new login cannot inherit disabled loading buttons.
  document.querySelectorAll("button,input,select,textarea").forEach(n=>{n.disabled=false;n.removeAttribute("aria-busy");});
  for(const f of document.querySelectorAll("form"))f.reset();
  document.querySelectorAll('input[type="search"]').forEach(n=>n.value="");
  $("account-filter").replaceChildren(new Option(translate("全部账号"), ""));
  $("state-filter").value="";$("protection-filter").value="";
  editingAccount=null;accountMode="add";pendingAction=null;
  for(const id of ["accounts","inventory","requests","target","resume-status","machine-detail","protections","result-meta","inventory-errors","action-description","auto-accounts"])$(id).replaceChildren();
  document.querySelectorAll(".feedback").forEach(n=>setText(n,""));
  setText($("output"),translate("尚无输出"));setText($("result-badge"),translate("尚未执行"));$("result-badge").className="badge";setText($("output-matches"),"");$("resume-panel").hidden=true;$("copy-output").disabled=true;
  $("workspace").hidden=true;$("login").hidden=false;updateExecute();closeNav();$("login-token").focus();
}
async function enterConsole(csrf) {
  state.epoch++; state.csrf=csrf; state.token="session";
  const data=await api("/api/admin/accounts");state.accounts=data.accounts;
  $("login").hidden=true;$("workspace").hidden=false;$("local-address").textContent=location.host;
  setText($("client-help"),"igw-client login\nigw-client machines\nigw-client --account ACCOUNT_ID --machine MACHINE_ID select\nigw-client --binding BINDING_ID exec -- printf hello");
  route();renderTarget();window.dispatchEvent(new Event("igw:login"));await refresh();
}
$("login-form").addEventListener("submit",async event=>{
  event.preventDefault();const f=event.target;
  const token=f.elements.token.value;f.elements.token.value="";
  await busy(f.querySelector("button"),async()=>{
    const response=await fetch("/api/auth/login",{method:"POST",credentials:"same-origin",headers:{Authorization:"Bearer "+token,"Content-Type":"application/json"},body:"{}"});
    if(!response.ok)throw new ApiError("authentication_required");
    await enterConsole((await response.json()).csrf);
  },"login-error");
});
async function signout(){await api("/api/auth/logout","POST",{});logout();}
$("logout").addEventListener("click",()=>{
  if(state.executing)confirmAction(translate("退出并清除本地页面"),[translate("当前命令可能继续在远端运行。退出不代表取消或终止；本次输出将无法从历史恢复。")],signout,translate("清除页面并退出"));else busy($("logout"),signout);
});
$("nav-toggle").addEventListener("click",()=>{
  const open=!$("sidebar").classList.contains("open");$("sidebar").classList.toggle("open",open);$("nav-toggle").setAttribute("aria-expanded",String(open));$("main").inert=open;if(open)$("nav-close").focus();
});
$("nav-close").addEventListener("click",()=>closeNav(true));
window.addEventListener("keydown",event=>{if(event.key === "Escape" && $("sidebar").classList.contains("open"))closeNav(true);});
window.addEventListener("resize",()=>{if(innerWidth>=1024)closeNav();});
window.addEventListener("hashchange",()=>route());
window.addEventListener("beforeunload",event=>{if(state.executing){event.preventDefault();event.returnValue="";}});
$("refresh").addEventListener("click",()=>busy($("refresh"),refresh));
$("add-account").addEventListener("click",()=>openAccount());
for(const id of ["account-filter","state-filter","protection-filter"])$(id).addEventListener("change",renderInventory);
$("machine-search").addEventListener("input",renderInventory);
$("refresh-requests").addEventListener("click",()=>busy($("refresh-requests"),loadRequests));
$("request-search").addEventListener("input",renderRequests);
$("workbench-tab").addEventListener("click",()=>setExecutionTab(false));$("history-tab").addEventListener("click",()=>setExecutionTab(true));
for(const channel of ["stdout","stderr"])$(channel+"-tab").addEventListener("click",()=>{state.channel=channel;renderOutput();});
$("output-search").addEventListener("input",renderOutput);
$("wrap-output").addEventListener("click",()=>{const wrap=$("output").classList.toggle("wrap");$("wrap-output").setAttribute("aria-pressed",String(wrap));setText($("wrap-output"),wrap ? translate("折行：开"):translate("折行：关"));});
$("copy-output").addEventListener("click",()=>confirmAction(translate("复制本次输出"),[translate("输出可能包含敏感内容，将写入系统剪贴板。请确认当前通道适合复制。")],async()=>{await navigator.clipboard.writeText(state.output?.result?.[state.channel] || "");notice(translate("已复制当前通道输出。"),"success");},translate("复制当前通道")));

// Language changes only redraw local UI; no API call or target reselection.
window.addEventListener("igw:languagechange", () => {
  if (!state.token) return;
  const filter = $("account-filter"), previous = filter.value;
  filter.replaceChildren(new Option(translate("全部账号"), ""), ...state.accounts.map(a => new Option(a.alias + " · " + a.id.slice(-6), a.id)));
  filter.value = previous;
  renderAccounts(); renderInventory(); renderTarget(); renderRequests(); renderProtections(); renderResume();
  if (state.output) renderResult();
  else if (!state.executing) renderOutput();
});

(async()=>{
  const ticket=location.hash.startsWith("#login=") ? location.hash.slice(7) : null;
  if(ticket)history.replaceState(null,"",location.pathname+"#instances");
  try{
    const response=await fetch(ticket ? "/api/auth/login" : "/api/auth/session",ticket ? {method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({ticket})} : {credentials:"same-origin"});
    if(response.ok)await enterConsole((await response.json()).csrf);
    else if(ticket)feedback("login-error",translate("登录链接已失效，请重新运行 igw open。"));
  }catch(_){feedback("login-error",translate("无法连接本地 API，请检查本地服务和端口；请求结果可能未知。"));}
})();
