"use strict";
(() => {
let binding=null, machine="", remotePath=".", localPath="", localParent="", localNext=null, remoteNext=null;
let localRead=0, remoteRead=0, inventoryRead=0, selectionRead=0, transferRead=0, timer=null;
const terminal=new Set(["succeeded","failed","cancelled"]), completed=new Set();
const kinds={directory:"目录",file:"文件",symlink:"符号链接：不可访问",other:"其他"};
const statuses={pending:"等待中",running:"下载中",succeeded:"已完成",failed:"失败",cancelled:"已取消"};
function size(bytes){
  if(bytes===null||bytes===undefined)return "—";
  const units=["B","KiB","MiB","GiB","TiB"];let unit=0;
  while(bytes>=1024&&unit<units.length-1){bytes/=1024;unit++;}
  return bytes.toLocaleString(window.I18n.locale,{maximumFractionDigits:unit?1:0})+" "+units[unit];
}
function entryRow(entry, open, download){
  const row=el("div",undefined,"file-entry"), info=el("div",undefined,"file-entry-info");
  if(entry.kind==="directory"){
    const b=button(entry.name,open,"file-folder");b.setAttribute("aria-label",String(translate("打开目录：{name}",{name:entry.name})));info.append(b);
  }else info.append(el("strong",entry.name));
  const modified=new Date(entry.modifiedAt*1000).toLocaleString(window.I18n.locale,{hour12:false});
  info.append(el("p",join(translate(kinds[entry.kind]||"其他")," · ",entry.kind==="file"?size(entry.bytes)+" · ":"",modified),"hint"));
  row.append(info);
  if(download)row.append(button(translate("下载到左侧"),download,"secondary",!localPath));
  return row;
}
async function local(path=localPath,offset=0){
  const request=++localRead;
  const value=await api("/api/admin/files/local?"+new URLSearchParams({...path?{path}:{},offset}));
  if(request!==localRead)return;
  localPath=value.path;localParent=value.parent;localNext=value.nextOffset;
  setText($("files-local-location"),translate("当前下载目录：{path}",{path:localPath}));
  $("files-local-path").value=localPath;$("files-local-up").disabled=localParent===localPath;
  $("files-local-more").hidden=localNext===null;$("files-local-list").replaceChildren();
  if(!value.entries.length)$("files-local-list").append(el("p",translate("目录为空。"),"hint"));
  for(const entry of value.entries){
    const separator=localPath.includes("\\")?"\\":"/";
    const child=localPath.replace(/[\\/]$/,"")+separator+entry.name;
    $("files-local-list").append(entryRow(entry,()=>local(child)));
  }
  feedback("files-local-feedback","");
}
async function remote(path=remotePath,offset=0){
  if(!binding)return;
  const request=++remoteRead, target=binding;
  const value=await api("/api/admin/files/remote?"+new URLSearchParams({bindingId:target.id,path,offset}));
  if(request!==remoteRead||binding?.id!==target.id)return;
  remotePath=value.path;remoteNext=value.nextOffset;
  $("files-remote-path").value=value.root+(remotePath==="."?"":"/"+remotePath);
  $("files-up").disabled=remotePath===".";$("files-more").hidden=remoteNext===null;
  setText($("files-location"),target.machineId+" · "+target.id);
  $("files-list").replaceChildren();
  if(!value.entries.length)$("files-list").append(el("p",translate("目录为空。"),"hint"));
  for(const entry of value.entries){
    const child=(remotePath==="."?"":remotePath+"/")+entry.name;
    $("files-list").append(entryRow(entry,()=>remote(child),entry.kind==="file"?()=>download(target,child):null));
  }
  feedback("files-remote-feedback","");
}
async function choose(){
  const request=++selectionRead, selected=$("files-machine").value;
  remoteRead++;binding=null;machine=selected;remotePath=".";$("files-list").replaceChildren();$("files-more").hidden=true;
  $("files-remote-path").value="/data";setText($("files-location"),"");
  if(!selected){setText($("files-machine-hint"),translate("请选择一台运行中的机器。"));return;}
  const value=await api("/api/v1/bindings","POST",{target:JSON.parse(selected)});
  if(request!==selectionRead||selected!==$("files-machine").value)return;
  binding=value.binding;setText($("files-machine-hint"),translate("固定当前机器；切换不影响已受理下载。"));
  await remote();
}
async function inventory(){
  const request=++inventoryRead, value=await api("/api/v1/machines");
  if(request!==inventoryRead)return;
  const options=[];
  for(const account of value.accounts){
    if(!account.complete||account.cached)continue;
    for(const m of account.machines){
      if(m.state!=="running"||m.protected)continue;
      options.push(new Option(`${account.alias} / ${m.name||m.id} · ${m.id}`,JSON.stringify({accountId:account.accountId,machineId:m.id})));
    }
  }
  $("files-machine").replaceChildren(new Option(String(translate("选择运行机器")),""),...options);
  if(options.some(o=>o.value===machine))$("files-machine").value=machine;
  else{machine="";binding=null;selectionRead++;remoteRead++;$("files-list").replaceChildren();$("files-more").hidden=true;setText($("files-location"),"");}
  setText($("files-machine-hint"),options.length?translate("请选择一台运行中的机器。"):translate("没有可用的运行机器。不会自动恢复暂停机器。"));
  feedback("files-feedback",value.complete?"":translate("部分账号读取失败；仅可选择本次读取成功的运行机器。"),"warning");
  if(binding)await remote();
}
async function download(target,path){
  if(!localPath)return;
  const directory=localPath;
  await api("/api/admin/files/downloads","POST",{bindingId:target.id,path,directory},{"Idempotency-Key":"download_"+crypto.randomUUID()});
  feedback("files-transfer-feedback",translate("下载已受理，目的地：{path}",{path:directory}),"success");
  await transfers();
}
async function transfers(){
  clearTimeout(timer);
  if(!state.token)return;
  const request=++transferRead, value=await api("/api/admin/files/downloads");
  if(request!==transferRead)return;
  const focused=document.activeElement?.dataset.fileCancel;
  $("files-transfers").replaceChildren();let refresh=false;
  const rows=value.downloads.sort((a,b)=>b.createdAt.localeCompare(a.createdAt));
  if(!rows.length)$("files-transfers").append(el("p",translate("暂无下载传输。"),"hint"));
  for(const row of rows){
    const card=el("article",undefined,"file-transfer"), head=el("div",undefined,"section-heading");
    head.append(el("strong",row.path.split("/").pop()),badge(translate(statuses[row.status]||"失败"),row.status==="succeeded"?"success":row.status==="failed"?"danger":""));
    card.append(head,el("p",row.machineId+" · "+row.path,"hint mono"),el("p",row.destination,"mono"));
    card.append(el("p",size(row.bytes)+" / "+size(row.expectedBytes),"hint mono"));
    if(!terminal.has(row.status)){
      const progress=el("progress");progress.setAttribute("aria-label",String(translate("下载进度")));
      if(row.expectedBytes){progress.max=row.expectedBytes;progress.value=row.bytes;}
      const cancel=button(translate(row.cancelRequested?"正在取消":"取消下载"),async()=>{await api(`/api/admin/files/downloads/${row.id}/cancel`,"POST",{});await transfers();},"ghost",row.cancelRequested);
      cancel.dataset.fileCancel=row.id;card.append(progress,cancel);
    }
    if(row.sha256)card.append(el("p","SHA256 · "+row.sha256,"hint mono"));
    if(row.error)card.append(el("p",message(new ApiError(row.error)),"feedback"));
    $("files-transfers").append(card);
    if(terminal.has(row.status)&&!completed.has(row.id)){completed.add(row.id);if(row.status==="succeeded"&&row.destination.startsWith(localPath))refresh=true;}
  }
  if(focused)$("files-transfers").querySelector(`[data-file-cancel="${focused}"]:not(:disabled)`)?.focus({preventScroll:true});
  if(refresh&&localPath)await local().catch(e=>feedback("files-local-feedback",message(e)));
  if(state.token&&location.hash==="#files"&&rows.some(r=>!terminal.has(r.status)))timer=setTimeout(()=>transfers().catch(e=>feedback("files-transfer-feedback",message(e))),1500);
}
async function load(){
  if(!state.token||location.hash!=="#files")return;
  await Promise.all([
    local().catch(e=>feedback("files-local-feedback",message(e))),
    inventory().catch(e=>feedback("files-remote-feedback",message(e))),
    transfers().catch(e=>feedback("files-transfer-feedback",message(e)))
  ]);
}
function wire(id,fn,errorId){$(id).addEventListener("click",()=>busy($(id),fn,errorId));}
$("files-machine").addEventListener("change",()=>choose().catch(e=>feedback("files-remote-feedback",message(e))));
$("files-local-form").addEventListener("submit",e=>{e.preventDefault();busy(e.submitter,()=>local($("files-local-path").value),"files-local-feedback");});
$("files-remote-form").addEventListener("submit",e=>{e.preventDefault();busy(e.submitter,async()=>{
  const path=$("files-remote-path").value;
  if(path!=="/data"&&!path.startsWith("/data/"))throw new ApiError("file_path_denied");
  await remote(path==="/data"?".":path.slice(6));
},"files-remote-feedback");});
wire("files-refresh",load,"files-feedback");
wire("files-local-up",()=>local(localParent),"files-local-feedback");
wire("files-local-home",()=>local(""),"files-local-feedback");
wire("files-local-refresh",()=>local(),"files-local-feedback");
wire("files-local-more",()=>local(localPath,localNext),"files-local-feedback");
wire("files-up",()=>remote(remotePath.includes("/")?remotePath.slice(0,remotePath.lastIndexOf("/")):"."),"files-remote-feedback");
wire("files-remote-root",()=>remote("."),"files-remote-feedback");
wire("files-remote-refresh",()=>remote(),"files-remote-feedback");
wire("files-more",()=>remote(remotePath,remoteNext),"files-remote-feedback");
wire("files-transfers-refresh",transfers,"files-transfer-feedback");
window.addEventListener("igw:login",load);
window.addEventListener("hashchange",()=>{clearTimeout(timer);load();});
window.addEventListener("igw:languagechange",load);
window.addEventListener("igw:logout",()=>{
  clearTimeout(timer);localRead++;remoteRead++;inventoryRead++;selectionRead++;transferRead++;
  binding=null;machine="";localPath="";localParent="";remotePath=".";completed.clear();
  for(const id of ["files-local-list","files-list","files-transfers","files-machine"])$(id).replaceChildren();
  for(const id of ["files-feedback","files-local-feedback","files-remote-feedback","files-transfer-feedback","files-machine-hint","files-location","files-local-location"])setText($(id),"");
  $("files-local-path").value="";$("files-remote-path").value="/data";
});
})();
