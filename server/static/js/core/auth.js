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
      case 'auth-force-pass':
        return submitForcePassword();
      case 'auth-force-logout':
        return Signage.logout();
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
      // Сервер различает причины: 401 — неверные данные; 403 — пользователь
      // заблокирован/деактивирован (показывается только при верном пароле,
      // чтобы не раскрывать статус учётки постороннему)
      let detail = '';
      try{ detail = (await res.json()).detail || ''; }catch(_){}
      err.textContent = (res.status === 403 && detail)
        ? detail + '. Обратитесь к администратору.'
        : 'Неверный логин или пароль';
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

//=============================================================================
// ФОРС-СМЕНА ПАРОЛЯ ПО УМОЛЧАНИЮ (must_change_password)
// Показывается сразу после входа, пока пароль admin не сменён. Панель не
// открывается, пока пароль по умолчанию не заменён.
//=============================================================================
function renderForcePassword(){
  document.getElementById('app').innerHTML = `
    <div class="login">
      <div style="font-size:16px;font-weight:600;margin-bottom:4px;">Смена пароля</div>
      <div class="muted" style="font-size:12px;margin-bottom:18px;">
        В целях безопасности смените пароль по умолчанию, прежде чем начать работу.</div>
      <div class="fld"><label>Текущий пароль</label><input class="inp" id="fp-old" type="password"></div>
      <div class="fld"><label>Новый пароль</label><input class="inp" id="fp-new" type="password" placeholder="мин. 6 символов"></div>
      <div class="fld"><label>Повторите новый пароль</label><input class="inp" id="fp-new2" type="password"></div>
      <div id="fp-err" class="danger" style="font-size:12px;min-height:16px;color:var(--danger);"></div>
      <button class="btn primary" style="width:100%;justify-content:center;margin-top:6px;" data-action="auth-force-pass">Сохранить и войти</button>
      <button class="btn" style="width:100%;justify-content:center;margin-top:6px;" data-action="auth-force-logout">Выйти</button>
    </div>`;

  document.getElementById('fp-new2').addEventListener('keydown', e => {
    if(e.key === 'Enter') submitForcePassword();
  });
}

async function submitForcePassword(){
  const oldp = document.getElementById('fp-old').value;
  const newp = document.getElementById('fp-new').value;
  const new2 = document.getElementById('fp-new2').value;
  const err  = document.getElementById('fp-err');
  err.textContent = '';

  if(newp.length < 6){ err.textContent = 'Новый пароль — минимум 6 символов'; return; }
  if(newp !== new2){ err.textContent = 'Пароли не совпадают'; return; }

  try{
    await api('/me/password', {
      method:'POST',
      headers:{ 'Content-Type':'application/json' },
      body: JSON.stringify({ old_password: oldp, new_password: newp })
    });
    boot();  // флаг снят на сервере — теперь загрузится сама панель
  }catch(e){
    err.textContent = 'Ошибка: ' + (e.message || 'не удалось сменить пароль');
  }
}

window.Signage = window.Signage || {};
window.Signage.doLogin = doLogin;
window.Signage.logout = logout;
window.renderForcePassword = renderForcePassword;

initAuthViewActions();

