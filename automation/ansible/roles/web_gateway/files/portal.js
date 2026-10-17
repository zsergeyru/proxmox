"use strict";
const page=document.body.dataset.page;
const q=(s)=>document.querySelector(s);
function text(node,value){node.textContent=String(value??"");}
function entries(node,rows,render){
  node.replaceChildren();
  if(!rows.length){const p=document.createElement("p");p.className="muted";text(p,"Пока нет записей.");node.append(p);return;}
  rows.forEach(v=>node.append(render(v)));
}
function el(tag,cls,content){const x=document.createElement(tag);if(cls)x.className=cls;if(content!==undefined)text(x,content);return x;}
async function request(path,body){
  const options={credentials:"same-origin",headers:{"Accept":"application/json","X-Requested-With":"web-gateway"}};
  if(body!==undefined){options.method="POST";options.headers["Content-Type"]="application/json";options.body=JSON.stringify(body);}
  const res=await fetch(path,options);const data=await res.json();
  if(!res.ok)throw Error(data.error||"Не удалось выполнить запрос");
  return data;
}
async function applications(){
  const holder=q("#apps");text(holder,"Загрузка…");
  try{
    const data=await request("/api/apps");
    if(!data.apps.length){text(holder,"Подключённые приложения появятся здесь после настройки и включения.");return;}
    entries(holder,data.apps,item=>{
      const a=el("a","tile");a.href=item.url;a.target="_blank";a.rel="noopener noreferrer";
      a.append(el("span","tile-icon",item.icon),el("strong","tile-title",item.name),el("span","tile-sub",item.description));return a;
    });
  }catch(e){text(holder,e.message);}
}
function notify(message){const box=q("#notification");text(box,message);}
async function users(){
  const data=await request("/api/admin/users");
  entries(q("#users-list"),data.users,item=>{
    const row=el("div","record");row.append(el("strong","",item.username+" — "+item.displayname),
      el("div","muted",(item.groups||[]).join(", ")+" · "+(item.disabled?"Отключён":"Доступен")));return row;
  });
}
async function routes(){
  const data=await request("/api/admin/routes");
  entries(q("#routes-list"),data.routes,item=>{
    const row=el("div","record");row.append(el("strong","",item.domain),
      el("div","muted",item.service+" · "+item.access+" · "+(item.enabled?"Включён":"Выключен")));return row;
  });
  const select=q("#route-domain");select.replaceChildren();
  data.routes.filter(x=>!["home.zsergey.ru","auth.zsergey.ru","photos.zsergey.ru"].includes(x.domain))
    .forEach(item=>{const option=document.createElement("option");option.value=item.domain;text(option,item.domain);select.append(option);});
}
async function guests(){
  const data=await request("/api/admin/guests");
  entries(q("#guests-list"),data.guests,item=>{
    const row=el("div","record");row.append(el("strong","",item.vmid+" — "+item.name),el("div","muted","Состояние: не проверено"));return row;
  });
}
async function showTab(name){
  document.querySelectorAll("[data-tab]").forEach(b=>b.classList.toggle("selected",b.dataset.tab===name));
  ["users","routes","guests"].forEach(n=>q("#"+n+"-panel").classList.toggle("hidden",n!==name));
  try{await ({users,routes,guests})[name]();}catch(e){notify(e.message);}
}
if(page==="home"){
  applications();
  q("#refresh").addEventListener("click",applications);
  request("/api/admin/access").then(()=>{q("#admin-link").hidden=false;}).catch(()=>{});
}
if(page==="admin"){
  document.querySelectorAll("[data-tab]").forEach(b=>b.addEventListener("click",()=>showTab(b.dataset.tab)));
  q("#user-form").addEventListener("submit",async event=>{
    event.preventDefault();const form=event.currentTarget,values=Object.fromEntries(new FormData(form));
    const body={action:values.action,username:values.username,name:values.name||"",
      groups:values.groups||"",confirm:form.elements.confirm.checked};
    try{const result=await request("/api/admin/users",body);notify(
      "Действие выполнено для "+result.username+(result.password?"\nНовый пароль (сохраните его): "+result.password:"")+
      (result.code?"\nОдноразовый код: "+result.code:""));
      await users();}catch(e){notify(e.message);}
  });
  q("#route-form").addEventListener("submit",async event=>{
    event.preventDefault();const form=event.currentTarget,values=Object.fromEntries(new FormData(form));
    const body={domain:values.domain,access:values.access,enabled:form.elements.enabled.checked,
      confirm:form.elements.confirm.checked};
    try{await request("/api/admin/routes",body);notify("Маршрут проверен и применён");await routes();}
    catch(e){notify(e.message);}
  });
  showTab("users");
}