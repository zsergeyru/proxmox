"use strict";
const page=document.body.dataset.page;
const q=(selector)=>document.querySelector(selector);
const text=(node,value)=>{node.textContent=String(value??"");};
const el=(tag,cls,content)=>{
  const node=document.createElement(tag);
  if(cls)node.className=cls;
  if(content!==undefined)text(node,content);
  return node;
};
function entries(node,rows,render){
  node.replaceChildren();
  if(!rows.length){node.append(el("p","muted","Ничего не найдено."));return;}
  rows.forEach(item=>node.append(render(item)));
}
async function request(path,body){
  const options={credentials:"same-origin",headers:{"Accept":"application/json","X-Requested-With":"web-gateway"}};
  if(body!==undefined){
    options.method="POST";options.headers["Content-Type"]="application/json";options.body=JSON.stringify(body);
  }
  const response=await fetch(path,options);
  const data=await response.json();
  if(!response.ok)throw Error(data.error||"Не удалось выполнить запрос");
  return data;
}
async function applications(){
  const holder=q("#apps");text(holder,"Загрузка…");
  try{
    const data=await request("/api/apps");
    if(!data.apps.length){text(holder,"Подключённые приложения появятся здесь после настройки и включения.");return;}
    entries(holder,data.apps,item=>{
      const a=el("a","tile");a.href=item.url;a.target="_blank";a.rel="noopener noreferrer";
      a.append(el("span","tile-icon",item.icon),el("strong","tile-title",item.name),el("span","tile-sub",item.description));
      return a;
    });
  }catch(error){text(holder,error.message);}
}
function notify(message,failed=false){
  const box=q("#notification");
  if(!box)return;
  text(box,message);box.hidden=!message;box.classList.toggle("error",failed);
}
let allUsers=[];
let editingUsername=null;
function credential(value,username,type){
  const panel=q("#credential-panel");
  text(q("#credential-heading"),type==="code"?"Проверочный код · "+username:"Новый пароль · "+username);
  text(q("#credential-description"),type==="code"?
    "Код показан полностью. Передайте его пользователю для подтверждения в Authelia.":
    "Пароль показан полностью. Скопируйте его: старый пароль восстановить из хеша нельзя.");
  text(q("#credential-value"),value);
  panel.hidden=false;
}
async function copyValue(value){
  try{await navigator.clipboard.writeText(value);notify("Значение скопировано.");}
  catch(error){notify("Не удалось скопировать автоматически. Выделите значение и скопируйте вручную.",true);}
}
async function copyCredential(){await copyValue(q("#credential-value").textContent);}
function userButton(label,action,user,variant="secondary"){
  const button=el("button",variant,label);
  button.type="button";
  button.addEventListener("click",()=>userAction(action,user,button));
  return button;
}
function userCard(item){
  const card=el("article","user-card");
  const header=el("div","user-card-header");
  const avatar=el("span","avatar",(item.displayname||item.username).trim().charAt(0).toUpperCase());
  avatar.setAttribute("aria-hidden","true");
  const heading=el("div","user-heading");
  heading.append(el("h3","",item.displayname||item.username),el("p","user-login","@"+item.username));
  const status=el("span","status-pill "+(item.disabled?"off":"on"),item.disabled?"Отключён":"Активен");
  header.append(avatar,heading,status);
  const groups=el("div","group-tags");
  (item.groups||[]).forEach(g=>groups.append(el("span","group-tag",({admins:"Администратор",family:"Семья",guests:"Гость"})[g]||g)));
  const factors=el("div","factor-details");
  const statusLine=el("p","factor-label","Google Authenticator: "+
    (item.totp===true?"Подключён":item.totp===false?"Не подключён":"Состояние не определено"));
  factors.append(statusLine);
  if(item.webauthn===true)factors.append(el("p","factor-label","Ключ WebAuthn подключён"));
  if(item.verification){
    const codeBox=el("div","inline-code");
    codeBox.append(el("span","code-title","Проверочный код: "));
    codeBox.append(el("code","totp-code",item.verification.value));
    const copy=el("button","secondary","Скопировать");
    copy.type="button";
    copy.addEventListener("click",()=>copyValue(item.verification.value));
    codeBox.append(copy);
    const timeLeft=Math.max(0,item.verification.expires_at-Math.floor(Date.now()/1000));
    factors.append(codeBox,el("p","code-expiry","Действует ещё "+Math.ceil(timeLeft/60)+" мин."));
  }else{
    factors.append(el("p","muted code-empty","Нет ожидающего кода. Запросите его в Authelia."));
  }
  const actions=el("div","user-actions");
  const edit=el("button","secondary","Изменить");edit.type="button";
  edit.addEventListener("click",()=>openUserForm(item));
  actions.append(edit);
  actions.append(userButton("Новый пароль","password",item));
  const refresh=el("button","secondary","Обновить код");refresh.type="button";
  refresh.addEventListener("click",async()=>{refresh.disabled=true;try{await users();notify("Данные TOTP обновлены.");}catch(e){notify(e.message,true);}finally{refresh.disabled=false;}});
  actions.append(refresh);
  actions.append(userButton("Сбросить TOTP","reset_totp",item));
  actions.append(userButton(item.disabled?"Включить":"Отключить",item.disabled?"enable":"disable",item));
  if(item.username!=="admin")actions.append(userButton("Удалить","delete",item,"danger"));
  card.append(header,groups,factors,actions);
  return card;
}
function renderUsers(){
  const query=q("#users-search").value.trim().toLocaleLowerCase("ru");
  const filtered=allUsers.filter(item=>(item.username+" "+item.displayname+" "+(item.groups||[]).join(" ")).toLocaleLowerCase("ru").includes(query));
  text(q("#users-count"),allUsers.length);
  entries(q("#users-list"),filtered,userCard);
}
async function users(){
  const data=await request("/api/admin/users");
  allUsers=data.users;
  renderUsers();
}
function openUserForm(item=null){
  editingUsername=item?item.username:null;
  const form=q("#user-form");
  form.reset();
  text(q("#user-dialog-title"),item?"Изменить пользователя":"Добавить пользователя");
  text(q("#save-user"),item?"Сохранить":"Создать");
  const username=form.elements.username;
  username.readOnly=!!item;
  username.value=item?item.username:"";
  form.elements.name.value=item?(item.displayname||item.username):"";
  const groups=item?item.groups:["family"];
  form.querySelectorAll('input[name="group"]').forEach(control=>{control.checked=groups.includes(control.value);});
  q("#user-form-error").hidden=true;
  q("#user-dialog").showModal();
  (item?form.elements.name:username).focus();
}
function closeUserForm(){q("#user-dialog").close();}
function askConfirmation(title,description,destructive=false){
  return new Promise(resolve=>{
    const dialog=q("#confirm-dialog");
    const apply=q("#apply-confirm");
    text(q("#confirm-title"),title);
    text(q("#confirm-description"),description);
    text(apply,destructive?"Да, удалить":"Продолжить");
    apply.classList.toggle("danger",destructive);
    const cleanup=()=>{
      apply.removeEventListener("click",yes);
      dialog.removeEventListener("close",cancel);
    };
    const yes=()=>{cleanup();dialog.close();resolve(true);};
    const cancel=()=>{cleanup();resolve(false);};
    apply.addEventListener("click",yes);
    dialog.addEventListener("close",cancel,{once:true});
    dialog.showModal();
  });
}
async function submitUserForm(event){
  event.preventDefault();
  const form=q("#user-form"),selected=[...form.querySelectorAll('input[name="group"]:checked')].map(x=>x.value);
  const error=q("#user-form-error");
  if(!selected.length){text(error,"Выберите хотя бы одну группу.");error.hidden=false;return;}
  const username=form.elements.username.value;
  const payload={action:editingUsername?"edit":"add",username,
    name:form.elements.name.value.trim(),groups:selected.join(","),confirm:false};
  const submit=q("#save-user");submit.disabled=true;
  try{
    const result=await request("/api/admin/users",payload);
    closeUserForm();
    notify(editingUsername?"Пользователь обновлён.":"Пользователь создан.");
    if(result.password)credential(result.password,username,"password");
    await users();
  }catch(e){text(error,e.message);error.hidden=false;}
  finally{submit.disabled=false;}
}
async function userAction(action,item,button){
  const username=item.username;
  const description={
    password:"Будет создан новый пароль пользователя «"+username+"». Старый пароль перестанет работать.",
    reset_totp:"Регистрация Google Authenticator пользователя «"+username+"» будет сброшена.",
    disable:"Вход пользователя «"+username+"» будет отключён.",
    delete:"Учётная запись «"+username+"» будет удалена. Вернуть её можно только из резервной копии."
  };
  if(description[action]){
    const approved=await askConfirmation(
      action==="delete"?"Удалить пользователя?":"Подтвердить действие",
      description[action],action==="delete");
    if(!approved)return;
  }
  button.disabled=true;
  try{
    const result=await request("/api/admin/users",{
      action,username,confirm:["password","disable","delete","reset_totp"].includes(action)
    });
    if(result.password)credential(result.password,username,"password");
    if(result.code)credential(result.code,username,"code");
    notify(({
      password:"Новый пароль создан.",
      code:"Проверочный код получен.",
      reset_totp:"Google Authenticator сброшен.",
      enable:"Вход разрешён.",
      disable:"Вход отключён.",
      delete:"Учётная запись удалена."
    })[action]||"Готово.");
    await users();
  }catch(error){notify(error.message,true);}
  finally{button.disabled=false;}
}
let allRoutes=[];
let editingRoute=null;
const accessLabels={public:"Служебный вход",protected:"Через Authelia",private:"Домашняя сеть / VPN",app_auth:"Вход в самом приложении",git_https:"Git HTTPS — вход Gogs"};
function routeCard(item){
  const card=el("article","route-card");
  const details=el("div","route-details");
  const title=el("div","route-title");
  title.append(el("h3","",item.name||item.service||item.domain),el("span","status-pill "+(item.enabled?"on":"off"),item.enabled?"Включён":"Выключен"));
  details.append(title,el("p","route-host",item.domain),
    el("p","route-upstream",item.upstream||"Адрес службы ещё не указан"),
    el("span","group-tag",accessLabels[item.access]||item.access));
  const tools=el("div","route-actions");
  const fixed=["home.zsergey.ru","auth.zsergey.ru"].includes(item.domain);
  const label=el("label","switch-field");
  const switcher=el("input");switcher.type="checkbox";switcher.checked=item.enabled;
  switcher.disabled=fixed||item.access==="app_auth";
  switcher.setAttribute("aria-label",(item.enabled?"Выключить ":"Включить ")+item.domain);
  const slider=el("span","switch-track");slider.setAttribute("aria-hidden","true");
  label.append(switcher,slider);
  switcher.addEventListener("change",async()=>{
    switcher.disabled=true;
    try{
      await request("/api/admin/routes",{action:"update",domain:item.domain,
        enabled:switcher.checked,access:item.access});
      await routes();notify("Домен «"+item.domain+"» обновлён.");
    }catch(error){
      switcher.checked=item.enabled;notify(error.message,true);
      switcher.disabled=false;
    }
  });
  tools.append(label);
  if(!fixed&&item.access!=="app_auth"){
    const edit=el("button","secondary","Изменить");edit.type="button";
    edit.addEventListener("click",()=>openRouteForm(item));tools.append(edit);
  }
  if(!item.managed){
    const remove=el("button","danger","Удалить");remove.type="button";
    remove.addEventListener("click",()=>removeRoute(item,remove));tools.append(remove);
  }
  if(fixed)tools.append(el("span","muted route-fixed","Защищённый системный домен"));
  card.append(details,tools);return card;
}
function renderRoutes(){
  const query=q("#routes-search").value.trim().toLocaleLowerCase("ru");
  const rows=allRoutes.filter(item=>(item.domain+" "+item.name+" "+item.upstream).toLocaleLowerCase("ru").includes(query));
  text(q("#routes-count"),allRoutes.length);
  entries(q("#routes-list"),rows,routeCard);
}
async function routes(){
  const data=await request("/api/admin/routes");allRoutes=data.routes;renderRoutes();
}
function openRouteForm(item=null){
  editingRoute=item;
  const form=q("#route-form");form.reset();
  text(q("#route-dialog-title"),item?"Изменить домен":"Добавить домен");
  const isGit=item?.managed && item.domain==="git.zsergey.ru" && item.service==="gogs";
  text(q("#route-help"),isGit?
    "Для Gogs укажите его внутренний адрес (например, http://192.168.1.50:3000). Git будет работать через HTTPS на порту 443 без Authelia.":
    item?.managed?
    "Этот домен описан в Git: можно изменить только доступ и состояние.":
    "Укажите поддомен zsergey.ru и внутренний IP-адрес приложения.");
  const fields=form.elements;
  fields.domain.value=item?item.domain:"";
  fields.domain.readOnly=!!item;
  fields.name.value=item?(item.name||item.service):"";
  fields.name.readOnly=!!item?.managed;
  fields.upstream.value=item?(item.upstream||""):"";
  fields.upstream.readOnly=!!item?.managed&&!isGit;
  fields.upstream.required=!item?.managed||isGit;
  fields.name.required=!item?.managed;
  form.querySelectorAll('input[name="access"]').forEach(input=>{
    input.disabled=isGit?input.value!=="git_https":input.value==="git_https";
    if(input.value==="git_https")input.closest("label").hidden=!isGit;
    input.checked=item?input.value===item.access:input.value==="protected";
  });
  fields.enabled.checked=item?item.enabled:false;
  text(q("#save-route"),item?"Сохранить изменения":"Добавить домен");
  q("#route-form-error").hidden=true;
  q("#route-dialog").showModal();
}
async function saveRoute(event){
  event.preventDefault();
  const form=q("#route-form");const fields=form.elements;
  const access=form.querySelector('input[name="access"]:checked')?.value;
  if(!access)return;
  const payload={action:editingRoute?"update":"create",
    domain:fields.domain.value.trim().toLowerCase(),enabled:fields.enabled.checked,access};
  if(!editingRoute||!editingRoute.managed){
    payload.name=fields.name.value.trim();
  }
  if(!editingRoute||!editingRoute.managed||editingRoute.domain==="git.zsergey.ru"){
    payload.upstream=fields.upstream.value.trim();
  }
  const button=q("#save-route"),error=q("#route-form-error");
  button.disabled=true;
  try{
    await request("/api/admin/routes",payload);
    q("#route-dialog").close();
    notify(editingRoute?"Домен обновлён.":"Новый домен добавлен.");
    await routes();
  }catch(e){text(error,e.message);error.hidden=false;}
  finally{button.disabled=false;}
}
async function removeRoute(item,button){
  const accepted=await askConfirmation("Удалить домен?",
    "Адрес «"+item.domain+"» исчезнет из Caddy и списка приложений. Остальные домены сохранятся.",true);
  if(!accepted)return;
  button.disabled=true;
  try{
    await request("/api/admin/routes",{action:"delete",domain:item.domain,confirm:true});
    notify("Домен удалён.");await routes();
  }catch(e){notify(e.message,true);}
  finally{button.disabled=false;}
}
async function guests(){
  const data=await request("/api/admin/guests");
  entries(q("#guests-list"),data.guests,item=>{
    const row=el("div","record");
    row.append(el("strong","",item.vmid+" — "+item.name),el("div","muted","Состояние: не проверено"));
    return row;
  });
}
async function showTab(name){
  document.querySelectorAll("[data-tab]").forEach(button=>{
    const selected=button.dataset.tab===name;
    button.classList.toggle("selected",selected);
    button.setAttribute("aria-selected",String(selected));
  });
  ["users","routes","guests"].forEach(value=>{
    const panel=q("#"+value+"-panel");
    panel.hidden=value!==name;panel.classList.toggle("hidden",value!==name);
  });
  notify("");
  try{await ({users,routes,guests})[name]();}
  catch(error){notify(error.message,true);}
}
function setupTheme(){
  const button=q("#theme-toggle");
  if(!button)return;
  try{
    const saved=localStorage.getItem("web-gateway-theme");
    if(saved==="light"||saved==="dark")document.documentElement.dataset.theme=saved;
  }catch(_error){/* Настройка темы необязательна. */}
  const refresh=()=>{text(button,document.documentElement.dataset.theme==="dark"?"☀ Светлая тема":"☾ Тёмная тема");};
  refresh();
  button.addEventListener("click",()=>{
    const next=document.documentElement.dataset.theme==="dark"?"light":"dark";
    document.documentElement.dataset.theme=next;
    try{localStorage.setItem("web-gateway-theme",next);}catch(_error){}
    refresh();
  });
}
if(page==="home"){
  applications();
  q("#refresh").addEventListener("click",applications);
  request("/api/admin/access").then(()=>{q("#admin-link").hidden=false;}).catch(()=>{});
}
if(page==="admin"){
  setupTheme();
  document.querySelectorAll("[data-tab]").forEach(b=>b.addEventListener("click",()=>showTab(b.dataset.tab)));
  q("#add-user").addEventListener("click",()=>openUserForm());
  q("#users-search").addEventListener("input",renderUsers);
  q("#user-form").addEventListener("submit",submitUserForm);
  q("#cancel-user").addEventListener("click",closeUserForm);
  q("#cancel-confirm").addEventListener("click",()=>q("#confirm-dialog").close());
  q("#copy-credential").addEventListener("click",copyCredential);
  q("#add-route").addEventListener("click",()=>openRouteForm());
  q("#routes-search").addEventListener("input",renderRoutes);
  q("#route-form").addEventListener("submit",saveRoute);
  q("#cancel-route").addEventListener("click",()=>q("#route-dialog").close());
  showTab("users");
}
