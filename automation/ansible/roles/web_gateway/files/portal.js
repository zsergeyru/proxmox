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
async function copyCredential(){
  const value=q("#credential-value").textContent;
  try{await navigator.clipboard.writeText(value);notify("Значение скопировано.");}
  catch(error){notify("Не удалось скопировать автоматически. Выделите значение и скопируйте вручную.",true);}
}
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
  const actions=el("div","user-actions");
  const edit=el("button","secondary","Изменить");edit.type="button";
  edit.addEventListener("click",()=>openUserForm(item));
  actions.append(edit);
  actions.append(userButton("Новый пароль","password",item));
  actions.append(userButton("Проверочный код","code",item));
  actions.append(userButton("Сбросить TOTP","reset_totp",item));
  actions.append(userButton(item.disabled?"Включить":"Отключить",item.disabled?"enable":"disable",item));
  if(item.username!=="admin")actions.append(userButton("Удалить","delete",item,"danger"));
  card.append(header,groups,actions);
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
async function routes(){
  const data=await request("/api/admin/routes");
  entries(q("#routes-list"),data.routes,item=>{
    const row=el("div","record");
    row.append(el("strong","",item.domain),el("div","muted",item.service+" · "+item.access+" · "+(item.enabled?"Включён":"Выключен")));
    return row;
  });
  const select=q("#route-domain");select.replaceChildren();
  data.routes.filter(x=>!["home.zsergey.ru","auth.zsergey.ru","photos.zsergey.ru"].includes(x.domain))
    .forEach(item=>{const option=el("option","",item.domain);option.value=item.domain;select.append(option);});
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
if(page==="home"){
  applications();
  q("#refresh").addEventListener("click",applications);
  request("/api/admin/access").then(()=>{q("#admin-link").hidden=false;}).catch(()=>{});
}
if(page==="admin"){
  document.querySelectorAll("[data-tab]").forEach(b=>b.addEventListener("click",()=>showTab(b.dataset.tab)));
  q("#add-user").addEventListener("click",()=>openUserForm());
  q("#users-search").addEventListener("input",renderUsers);
  q("#user-form").addEventListener("submit",submitUserForm);
  q("#cancel-user").addEventListener("click",closeUserForm);
  q("#cancel-confirm").addEventListener("click",()=>q("#confirm-dialog").close());
  q("#copy-credential").addEventListener("click",copyCredential);
  q("#route-form").addEventListener("submit",async event=>{
    event.preventDefault();const form=event.currentTarget,values=Object.fromEntries(new FormData(form));
    const body={domain:values.domain,access:values.access,enabled:form.elements.enabled.checked,
      confirm:form.elements.confirm.checked};
    try{await request("/api/admin/routes",body);notify("Маршрут проверен и применён");await routes();}
    catch(error){notify(error.message,true);}
  });
  showTab("users");
}
