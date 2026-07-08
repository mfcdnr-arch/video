//=============================================================================
// ЛОГИН
//=============================================================================
function initAuthViewActions(){
  if(window.__authViewActionsInitialized) return;
  window.__authViewActionsInitialized = true;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('auth-')) return;

    switch(action){
      case 'auth-login':
        return Signage.doLogin();
    }
  });
}

function renderLogin(){
  document.getElementById('app').innerHTML = `
    <div class="login">
      <div style="font-size:16px;font-weight:600;margin-bottom:4px;">Digital Signage</div>
      <div class="muted" style="font-size:12px;margin-bottom:18px;">Вход в админ-панель</div>
      <div class="fld"><label>Логин</label><input class="inp" id="lg-user" value="admin"></div>
      <div class="fld"><label>Пароль</label><input class="inp" id="lg-pass" type="password" placeholder="пароль"></div>
      <div id="lg-err" class="danger" style="font-size:12px;min-height:16px;color:var(--danger);"></div>
      <button class="btn primary" style="width:100%;justify-content:center;margin-top:6px;" data-action="auth-login">Войти</button>
    </div>`;

  document.getElementById('lg-pass').addEventListener('keydown', e => {
    if(e.key === 'Enter') Signage.doLogin();
  });
}

async function doLogin(){
  const u = document.getElementById('lg-user').value;
  const p = document.getElementById('lg-pass').value;
  const err = document.getElementById('lg-err');
  err.textContent = '';

  try{
    const body = new URLSearchParams({ username: u, password: p });
    const res = await fetch(API + '/token', { method: 'POST', body });

    if(!res.ok){
      err.textContent = 'Неверный логин или пароль';
      return;
    }

    const data = await res.json();
    TOKEN = data.access_token;
    localStorage.setItem('ds_token', TOKEN);
    boot();
  }catch(e){
    err.textContent = 'Ошибка соединения с сервером';
  }
}

function logout(){
  TOKEN = null;
  localStorage.removeItem('ds_token');
  renderLogin();
}

window.Signage = window.Signage || {};
window.Signage.doLogin = doLogin;
window.Signage.logout = logout;

initAuthViewActions();

