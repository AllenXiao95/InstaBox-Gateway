"use strict";
(() => {
Object.assign(errors,{
  workspace_probe_failed:translate("工作区目录或仓库检查未通过，请核对路径、符号链接和机器状态。"),
  workspace_path_denied:translate("路径必须在固定工作区内，不能通过符号链接访问外部。"),
  workspace_target_conflict:translate("工作区已固定目标，不能同时指定其他账号或机器。"),
  checkpoint_resume_not_configured:translate("此任务未声明检查点续跑命令，不能自动重放。"),
  external_task_readonly:translate("此任务为只读接管，请使用原执行器控制。"),
  task_still_running:translate("原任务尚未结束，请先查询原记录。")
});
let workspaces=[], tasks=[], events=[], selected=null, logOffset=0, logChannel="stdout", reading=false;
const labels={pending:"等待启动",running:"运行中",succeeded:"执行成功",failed:"失败",cancelled:"已取消",blocked:"需要处理",verified:"验证通过",not_requested:"未要求验证",unavailable:"暂不可达",available:"可达",stale:"观测已过期",submitted:"已提交桌面通知"};
const label=value=>translate(labels[value]||value);
const argv=id=>{
  const value=JSON.parse($(id).value);
  if(!Array.isArray(value)||!value.length||!value.every(x=>typeof x==="string"))throw new ApiError("invalid_argv");
  return value;
};
function render(){
  const previous=$("task-workspace").value;
  $("task-workspace").replaceChildren(new Option(translate("选择工作区"),""),...workspaces.map(w=>new Option(w.name+" · "+w.id,w.id)));
  if(workspaces.some(w=>w.id===previous))$("task-workspace").value=previous;
  $("task-empty").hidden=tasks.length>0;
  $("task-list").replaceChildren(...tasks.map(task=>{
    const row=el("article",undefined,"task-card"), workspace=workspaces.find(w=>w.id===task.workspaceId);
    row.append(el("strong",task.id,"mono"),el("p",workspace ? workspace.name+" · "+workspace.remoteRoot : task.workspaceId,"mono"));
    if(task.machineId)row.append(el("p",alias(task.accountId)+" / "+task.machineId,"mono"));
    row.append(el("p",join(label(task.status)," · ",label(task.verification)," · ",label(task.observation)),task.status==="failed"||task.status==="blocked" ? "feedback warning":"muted"));
    row.append(el("p",join(translate("上次观察："),date(task.lastObservedAt)),"muted"));
    const event=events.find(e=>e.id===task.id);
    if(event)row.append(el("p",join(translate("桌面通知："),label(event.desktop)),"muted"));
    const controls=actions(button(translate("查看日志"),()=>open(task.id)));
    if(["pending","running"].includes(task.status)&&task.source!=="external")controls.append(button(translate("取消任务"),()=>confirmAction(translate("取消任务"),[translate("仅终止本任务进程组；Docker 等外部资源需要单独核对。")],async()=>{await api(`/api/v1/tasks/${task.id}/cancel`,"POST",{});await load();},translate("取消任务")),"secondary"));
    if(["succeeded","failed","cancelled","blocked"].includes(task.status)){
      for(const [action,configured,text] of [["resume",task.resumeConfigured,"继续检查点任务"],["verify",task.verifyConfigured,"再次验证"]]){
        if(configured)controls.append(button(translate(text),async()=>{
          const key=`${task.id}_${action}`;
          await api(`/api/v1/tasks/${task.id}/${action}`,"POST",{},{"Idempotency-Key":key});await load();
        },"secondary"));
      }
    }
    if(event&&!event.read)controls.append(button(translate("标为已读"),async()=>{await api(`/api/v1/task-events/${task.id}/read`,"POST",{});await load();},"secondary"));
    row.append(controls);return row;
  }));
}
async function load(){
  if(!state.token||reading)return;
  reading=true;
  try{
    const result=await Promise.all([api("/api/v1/workspaces"),api("/api/v1/tasks"),api("/api/v1/task-events")]);
    workspaces=result[0].workspaces;tasks=result[1].tasks;events=result[2].events;
    render();feedback("tasks-feedback",translate("后台监视由本地 Gateway 执行；电脑休眠或服务停止时通知会延迟。"),"info");
  }catch(error){if(error.code!=="stale_session")feedback("tasks-feedback",message(error));}
  finally{reading=false;}
}
async function open(id){
  selected=id;logOffset=0;setText($("task-log"),"");$("task-detail").hidden=false;
  const task=await api(`/api/v1/tasks/${id}`),workspace=workspaces.find(w=>w.id===task.workspaceId);
  $("task-summary").replaceChildren(el("p",task.id,"mono"),el("p",join(label(task.status)," · ",label(task.verification))),el("p",workspace ? workspace.name+" · "+workspace.remoteRoot : task.workspaceId,"mono"),el("p",join(translate("上次观察："),date(task.lastObservedAt))));
  setText($("task-state"),JSON.stringify(task,null,2));
  await logs();
}
async function logs(){
  if(!selected)return;
  const id=selected,channel=logChannel,epoch=state.epoch;
  const value=await api(`/api/v1/tasks/${id}/logs?channel=${channel}&offset=${logOffset}`);
  if(epoch!==state.epoch||id!==selected||channel!==logChannel)return;
  // Show a bounded page; the offset remains available for further reads.
  $("task-log").textContent=($("task-log").textContent+value.text).slice(-131072);logOffset=value.offset;
}
$("workspace-form").addEventListener("submit",event=>{
  event.preventDefault();const form=event.target;
  busy(form.querySelector("button"),async()=>{
    if(!state.target)throw new ApiError("binding_not_found");
    const value=await api("/api/v1/workspaces","POST",{bindingId:state.target.id,name:form.elements.name.value,remoteRoot:form.elements.remoteRoot.value,localRoot:form.elements.localRoot.value});
    await load();$("task-workspace").value=value.id;
  },"tasks-feedback");
});
$("task-form").addEventListener("submit",event=>{
  event.preventDefault();const form=event.target;
  busy(form.querySelector("button"),async()=>{
    const body={mode:"background_task",workspaceId:$("task-workspace").value,argv:argv("task-argv")};
    if($("task-verify").value.trim())body.verifyArgv=argv("task-verify");
    // Keep the same key on a lost response. Change inputs deliberately to create a new intent.
    const fingerprint=JSON.stringify(body);
    if(form.dataset.fingerprint!==fingerprint){form.dataset.fingerprint=fingerprint;form.dataset.submission=crypto.randomUUID();}
    const value=await api("/api/v1/tasks","POST",body,{"Idempotency-Key":form.dataset.submission});
    feedback("task-submit-status",join(translate("任务已登记："),value.id),"success");
    if(value.dispatch==="confirmed"||value.dispatch==="not_started"){
      delete form.dataset.fingerprint;delete form.dataset.submission;
    }
    await load();
  },"tasks-feedback");
});
$("refresh-tasks").addEventListener("click",()=>busy($("refresh-tasks"),load,"tasks-feedback"));
for(const channel of ["stdout","stderr"])$("task-"+channel).addEventListener("click",()=>busy($("task-"+channel),async()=>{if(logChannel!==channel){logChannel=channel;logOffset=0;setText($("task-log"),"");}await logs();},"tasks-feedback"));
window.addEventListener("igw:login",load);
window.addEventListener("igw:languagechange",render);
window.addEventListener("hashchange",()=>{if(location.hash==="#tasks")load();});
window.addEventListener("igw:logout",()=>{workspaces=[];tasks=[];events=[];selected=null;$("task-list").replaceChildren();setText($("task-state"),"");setText($("task-log"),"");$("task-detail").hidden=true;delete $("task-form").dataset.fingerprint;delete $("task-form").dataset.submission;});
setInterval(()=>{if(state.token&&location.hash==="#tasks"&&!document.hidden)load();},10000);
})();
