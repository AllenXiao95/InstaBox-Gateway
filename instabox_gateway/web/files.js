"use strict";
(() => {
let workspaces=[], path=".", next=null, persistentBinding=null, read=0;
async function load(){
  if(!state.token)return;
  try{
    const previous=$("files-workspace").value;
    if(persistentBinding?.bindingId!==state.target?.id)path=".";
    persistentBinding=state.target?{bindingId:state.target.id,remoteRoot:state.settings?.fileRoot||""}:null;
    workspaces=(await api("/api/v1/workspaces")).workspaces;
    $("files-workspace").replaceChildren(new Option(String(translate("选择工作区")),""),...(state.target?[new Option(String(translate("当前固定目标的持久化根目录")),"persistent")]:[]),...workspaces.map(w=>new Option(w.name,w.id)));
    if(workspaces.some(w=>w.id===previous)||previous==="persistent"&&state.target)$("files-workspace").value=previous;
    if(!$("files-workspace").value&&state.target)$("files-workspace").value="persistent";
    if($("files-workspace").value)await list();
  }catch(e){feedback("files-feedback",message(e));}
}
async function list(offset=0){
  const id=$("files-workspace").value, persistent=id==="persistent";
  const request=++read, requestedPath=path;
  const workspace=persistent?persistentBinding:workspaces.find(w=>w.id===id);
  $("files-list").replaceChildren();$("files-more").hidden=true;
  if(!workspace){setText($("files-location"),translate("请先在后台任务页登记工作区。"));return;}
  setText($("files-location"),workspace.remoteRoot+" · "+workspace.bindingId);
  const value=await api(persistent?"/api/v1/persistent-files?"+new URLSearchParams({bindingId:workspace.bindingId,path,offset}):`/api/v1/workspaces/${id}/files?`+new URLSearchParams({path,offset}));
  if(request!==read||id!==$("files-workspace").value)return;
  path=requestedPath;
  setText($("files-location"),value.root+" · "+workspace.bindingId);
  setText($("files-path"),path);$("files-up").disabled=path===".";
  next=value.nextOffset;$("files-more").hidden=next===null;
  if(!value.entries.length)$("files-list").append(el("p",translate("目录为空。"),"hint"));
  for(const entry of value.entries){
    const relative=(path==="."?"":path+"/")+entry.name;
    const row=el("div",undefined,"resource-row"), info=el("div");
    info.append(el("strong",entry.name),el("p",`${entry.kind} · ${entry.bytes.toLocaleString()} bytes`,"hint mono"));
    const controls=actions();
    if(entry.kind==="directory")controls.append(button(translate("打开目录"),async()=>{path=relative;await list();}));
    if(entry.kind==="file"){
      // Native navigation streams directly to the browser download manager.
      // The HttpOnly session authenticates the GET without putting a token in URL.
      const a=el("a",translate("下载"),"button-link");a.href=persistent?"/api/v1/persistent-file?"+new URLSearchParams({bindingId:workspace.bindingId,path:relative,download:"1"}):"/api/v1/file?"+new URLSearchParams({workspaceId:id,path:relative,download:"1"});a.setAttribute("download",entry.name);controls.append(a);
    }
    if(entry.kind==="symlink")controls.append(badge(translate("符号链接：不可访问")));
    row.append(info,controls);$("files-list").append(row);
  }
  feedback("files-feedback",translate("目录已更新；下载由浏览器管理。"),"success");
}
$("files-workspace").addEventListener("change",()=>{path=".";busy($("files-refresh"),()=>list(),"files-feedback");});
$("files-refresh").addEventListener("click",()=>busy($("files-refresh"),load,"files-feedback"));
$("files-up").addEventListener("click",()=>busy($("files-up"),async()=>{path=path.includes("/")?path.slice(0,path.lastIndexOf("/")):".";await list();},"files-feedback"));
$("files-more").addEventListener("click",()=>busy($("files-more"),()=>list(next),"files-feedback"));
window.addEventListener("igw:login",load);
window.addEventListener("hashchange",()=>{if(location.hash==="#files")load();});
window.addEventListener("igw:languagechange",()=>{if(state.token&&location.hash==="#files")load();});
window.addEventListener("igw:logout",()=>{read++;workspaces=[];path=".";next=null;persistentBinding=null;$("files-list").replaceChildren();$("files-workspace").replaceChildren();setText($("files-location"),"");setText($("files-path"),"");});
})();
