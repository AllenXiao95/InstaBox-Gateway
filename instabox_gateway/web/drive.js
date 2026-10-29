"use strict";
(() => {
let connected=null,selections=[],workspaces=[],imports=[],uploads=[],parents=[{id:"root",name:"My Drive"}],shared=false,page=null,readSelection=null,uploadIntent=null;
const FOLDER="application/vnd.google-apps.folder";
const errorText=error=>translate("Google Drive 操作未完成：{code}",{code:error.code||"request_failed"});
function report(e){feedback("drive-feedback",errorText(e));}
async function run(control,action){await busy(control,async()=>{try{await action();}catch(e){if(e.code!=="stale_session")report(e);}},"drive-feedback");}
function options(id,values){const select=$(id),old=select.value;select.replaceChildren(new Option(String(translate("请选择")),""),...values.map(v=>new Option(v.name,v.id)));if(values.some(v=>v.id===old))select.value=old;}
async function load(){
 if(!state.token)return;
 try{
  const [status,selected,spaces,jobs]=await Promise.all([api("/api/admin/drive"),api("/api/v1/drive/selections"),api("/api/v1/workspaces"),api("/api/v1/drive/imports")]);
  connected=status;selections=selected.selections;workspaces=spaces.workspaces;imports=jobs.imports;uploads=jobs.uploads;
  render();
 }catch(e){if(e.code!=="stale_session")report(e);}
}
function render(){
 if(!connected)return;
 setText($("drive-connection"),join(connected.connected?translate("已连接"):translate("未连接")," · ",connected.user?.emailAddress||connected.oauthStatus));
 $("drive-setup").open=!connected.configured;$("drive-connect").disabled=!connected.configured||connected.oauthStatus==="waiting";
 $("drive-disconnect").disabled=!connected.connected&&connected.oauthStatus!=="waiting";
 $("drive-browser-panel").hidden=!connected.connected;
 if(connected.oauthStatus==="connected"){$("drive-auth-link").hidden=true;$("drive-auth-link").removeAttribute("href");}
 options("drive-read-selection",selections.filter(s=>s.access==="read"));options("drive-upload-selection",selections.filter(s=>s.access==="upload"));options("drive-workspace",workspaces);
 $("drive-selections").replaceChildren();
 if(!selections.length)$("drive-selections").append(el("p",translate("尚未批准任何内容。"),"hint"));
 for(const s of selections){
  const row=el("div",undefined,"resource-row"),info=el("div");info.append(el("strong",s.name),el("p",`${s.access} · ${s.id}`,"mono hint"));
  const controls=actions();if(s.access==="read")controls.append(button(translate("在线读取"),()=>show(s)));
  controls.append(button(translate("移除授权"),()=>confirmAction(translate("移除授权"),[translate("执行客户端将不能继续访问这项选择；已下载副本保留。")],async()=>{await api(`/api/admin/drive/selections/${s.id}`,"DELETE");await load();},translate("移除授权"))));row.append(info,controls);$("drive-selections").append(row);
 }
 $("drive-imports").replaceChildren();
 for(const job of imports){
  const row=el("div",undefined,"resource-row"),info=el("div"),done=job.files.filter(f=>f.status==="verified").length;
  info.append(el("strong",job.id),el("p",`${job.status} · ${done}/${job.files.length} · ${job.error||job.verification}`,"hint"),el("p",job.destination,"mono hint"));const controls=actions();
  if(["pending","running"].includes(job.status))controls.append(button(translate("取消任务"),async()=>{await api(`/api/v1/drive/imports/${job.id}/cancel`,"POST",{});await load();}));
  if(["failed","cancelled","blocked"].includes(job.status))controls.append(button(translate("续跑"),async()=>{await api(`/api/v1/drive/imports/${job.id}/resume`,"POST",{});await load();}));
  row.append(info,controls);$("drive-imports").append(row);
 }
 $("drive-uploads").replaceChildren();
 for(const job of uploads){const row=el("div",undefined,"resource-row"),info=el("div");info.append(el("strong",job.name),el("p",`${job.status} · ${job.fileId} · ${job.error||job.sha256}`,"mono hint"));const controls=actions();if(job.status!=="verified")controls.append(button(translate("核对原上传"),async()=>{await api(`/api/v1/drive/uploads/${job.id}/verify`,"POST",{});await load();}));row.append(info,controls);$("drive-uploads").append(row);}
}
async function browse(token=null){
 const query=new URLSearchParams(shared&&parents.length===1?{shared:"true"}:{parentId:parents.at(-1).id});if(token)query.set("pageToken",token);
 const value=await api("/api/admin/drive/files?"+query);page=value.nextPageToken||null;$("drive-more").hidden=!page;
 setText($("drive-breadcrumb"),parents.map(p=>p.name).join(" / "));$("drive-back").disabled=parents.length===1;$("drive-browser").replaceChildren();
 if(!value.files.length)$("drive-browser").append(el("p",translate("目录为空。"),"hint"));
 for(const file of value.files){
  const row=el("div",undefined,"resource-row"),info=el("div");info.append(el("strong",file.name),el("p",file.mimeType,"hint"));const controls=actions();
  if(file.mimeType===FOLDER)controls.append(button(translate("打开目录"),async()=>{parents.push({id:file.id,name:file.name});await browse();}));
  if(file.mimeType!=="application/vnd.google-apps.shortcut")controls.append(button(translate("批准读取"),async()=>{await api("/api/admin/drive/selections","POST",{fileId:file.id,access:"read"});await load();feedback("drive-feedback",translate("读取范围已保存。"),"success");}));
  if(file.mimeType===FOLDER&&file.capabilities?.canAddChildren)controls.append(button(translate("设为上传文件夹"),async()=>{
   if(connected.uploadEnabled&&file.isAppAuthorized){await api("/api/admin/drive/selections","POST",{fileId:file.id,access:"upload"});await load();}
   else{const value=await api("/api/admin/drive/connect","POST",{folderId:file.id});const link=$("drive-auth-link");link.href=value.authorizationUrl;link.hidden=false;link.focus();link.scrollIntoView({block:"center"});feedback("drive-feedback",translate("请在 Google Picker 中确认同一账号和目标文件夹，然后刷新连接。"),"info");}
  }));
  row.append(info,controls);$("drive-browser").append(row);
 }
}
async function show(s,parent=null,token=null){
 readSelection=s;$("drive-read-panel").hidden=false;setText($("drive-read-title"),join(translate("在线读取")," · ",s.name));$("drive-read-list").replaceChildren();setText($("drive-preview"),"");
 const folder=parent||s.fileId;
 if(s.mimeType!==FOLDER){renderFile(s,{id:s.fileId,name:s.name,mimeType:s.mimeType});return;}
 const value=await api(`/api/v1/drive/selections/${s.id}/files?`+new URLSearchParams({parentId:folder,...(token?{pageToken:token}:{})}));
 if(folder!==s.fileId)$("drive-read-list").append(button(translate("返回所选根目录"),()=>show(s)));
 for(const file of value.files){if(file.mimeType===FOLDER)$("drive-read-list").append(button(file.name,()=>show(s,file.id)));else renderFile(s,file);}
 if(value.nextPageToken)$("drive-read-list").append(button(translate("下一页"),()=>show(s,folder,value.nextPageToken)));
}
function renderFile(s,file){
 const row=el("div",undefined,"resource-row"),controls=actions();row.append(el("strong",file.name));
 const url=`/api/v1/drive/selections/${s.id}/content?`+new URLSearchParams({fileId:file.id});
 if(file.mimeType.startsWith("text/")||file.mimeType==="application/json"||file.mimeType==="application/vnd.google-apps.document")controls.append(button(translate("读取最新内容"),async()=>{
  const epoch=state.epoch,response=await fetch(url+"&preview=1",{credentials:"same-origin",cache:"no-store",headers:state.token==="session"?{}:{Authorization:"Bearer "+state.token}});
  if(!response.ok){const e=await response.json();throw new ApiError(e.error?.code);}
  const value=await response.json();if(epoch!==state.epoch)return;
  $("drive-preview").textContent=value.text+(value.truncated?"\n"+String(translate("预览已截断；下载可获取完整文件。")):"");
 }));
 const a=el("a",translate("下载"),"button-link");a.href=url;a.setAttribute("download",file.name);controls.append(a);row.append(controls);$("drive-read-list").append(row);
}
$("drive-config-form").addEventListener("submit",event=>{event.preventDefault();run(event.target.querySelector("button"),async()=>{const file=$("drive-config").files[0];if(!file||file.size>65536)throw new ApiError("drive_desktop_client_required");const config=JSON.parse(await file.text());$("drive-config").value="";await api("/api/admin/drive/config","PUT",config);await load();});});
$("drive-connect").addEventListener("click",()=>run($("drive-connect"),async()=>{const value=await api("/api/admin/drive/connect","POST",{allowUpload:false});const link=$("drive-auth-link");link.href=value.authorizationUrl;link.hidden=false;setText($("drive-connection"),translate("请在 Google 完成授权，然后刷新连接。"));}));
$("drive-disconnect").addEventListener("click",()=>confirmAction(translate("断开本地连接"),[translate("清除本地 Google 令牌；停止后续读取与传输。Google 账号授权可在其安全设置中撤销。")],async()=>{await api("/api/admin/drive/disconnect","POST",{});$("drive-auth-link").hidden=true;$("drive-auth-link").removeAttribute("href");$("drive-browser").replaceChildren();$("drive-read-panel").hidden=true;setText($("drive-preview"),"");await load();}));
$("drive-refresh").addEventListener("click",()=>run($("drive-refresh"),load));
for(const id of ["drive-root","drive-shared"])$(id).addEventListener("click",()=>run($(id),async()=>{shared=id==="drive-shared";parents=[{id:"root",name:String(translate(shared?"与我共享":"我的云端硬盘"))}];await browse();}));
$("drive-back").addEventListener("click",()=>run($("drive-back"),async()=>{if(parents.length>1)parents.pop();await browse();}));
$("drive-more").addEventListener("click",()=>run($("drive-more"),()=>browse(page)));
$("drive-import-form").addEventListener("submit",event=>{event.preventDefault();run(event.target.querySelector("button"),async()=>{const body={selectionId:$("drive-read-selection").value,workspaceId:$("drive-workspace").value};const form=event.target,fingerprint=JSON.stringify(body);if(form.dataset.fingerprint!==fingerprint){form.dataset.fingerprint=fingerprint;form.dataset.key=crypto.randomUUID();}await api("/api/v1/drive/imports","POST",body,{"Idempotency-Key":form.dataset.key});delete form.dataset.fingerprint;delete form.dataset.key;await load();feedback("drive-feedback",translate("快照已登记，可关闭页面。"),"success");});});
$("drive-upload-form").addEventListener("submit",event=>{event.preventDefault();run(event.target.querySelector("button"),async()=>{
 const file=$("drive-upload-file").files[0],selection=$("drive-upload-selection").value;if(!file)throw new ApiError("drive_file_required");
 if(!uploadIntent||uploadIntent.file!==file||uploadIntent.selection!==selection)uploadIntent={file,selection,key:crypto.randomUUID()};
 const epoch=state.epoch,response=await fetch(`/api/v1/drive/selections/${selection}/upload?`+new URLSearchParams({name:file.name}),{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/octet-stream","X-Gateway-CSRF":state.csrf,"Idempotency-Key":uploadIntent.key},body:file});
 const value=await response.json();if(epoch!==state.epoch)return;if(!response.ok)throw new ApiError(value.error?.code);
 if(value.status==="verified"){uploadIntent=null;$("drive-upload-file").value="";feedback("drive-feedback",translate("上传已验证。"),"success");}else feedback("drive-feedback",translate("上传结果未知，请核对原上传记录。"));
 await load();
 });});
window.addEventListener("igw:login",load);window.addEventListener("hashchange",()=>{if(location.hash==="#drive")load();});window.addEventListener("igw:languagechange",render);
window.addEventListener("igw:logout",()=>{connected=null;selections=[];workspaces=[];imports=[];uploads=[];readSelection=null;uploadIntent=null;parents=[{id:"root",name:"My Drive"}];shared=false;page=null;for(const id of ["drive-selections","drive-imports","drive-uploads","drive-browser","drive-read-list"])$(id).replaceChildren();setText($("drive-preview"),"");$("drive-read-panel").hidden=true;$("drive-browser-panel").hidden=true;$("drive-auth-link").hidden=true;$("drive-auth-link").removeAttribute("href");});
setInterval(()=>{if(state.token&&!document.hidden&&location.hash==="#drive"&&!$("page-drive").contains(document.activeElement)&&(connected?.oauthStatus==="waiting"||imports.some(t=>["pending","running"].includes(t.status))))load();},5000);
})();
