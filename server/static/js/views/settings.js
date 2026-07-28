//=============================================================================
// НАСТРОЙКИ
//=============================================================================

async function viewSettings(){
  const view = document.getElementById('view');

  let usersBlock = '';
  if(isSuper()){
    usersBlock = `<div class="sec">Пользователи</div>
      <div id="users-list" class="muted">Загрузка…</div>
      <div class="sec" style="font-size:12px;">Добавить пользователя</div>
      <div class="row">
        <div class="fld" style="flex:1;"><label>Фамилия</label><input class="inp" id="nu-lastname" placeholder="Иванов"></div>
        <div class="fld" style="flex:1;"><label>Имя</label><input class="inp" id="nu-firstname" placeholder="Иван"></div>
      </div>
      <div class="row">
        <div class="fld" style="flex:1;"><label>Логин</label><input class="inp" id="nu-login" placeholder="ivanov"></div>
        <div class="fld" style="flex:1;"><label>Пароль</label><input class="inp" id="nu-pass" type="password" placeholder="мин. 6 символов"></div>
        <div class="fld" style="flex:1;"><label>Роль</label><select class="inp" id="nu-role" data-action="settings-role-changed"><option value="auditor">Аудитор (просмотр и отчёты)</option><option value="moderator">Модератор (проверка рекламы)</option><option value="admin">Админ (все права)</option><option value="advertiser">Рекламодатель (только свой кабинет)</option></select></div>
      </div>
      <div class="row" id="nu-adv-row" style="display:none;">
        <div class="fld" style="flex:1;"><label>Наименование рекламодателя</label>
          <input class="inp" id="nu-advname" placeholder="Кофейня «Утро»">
          <div class="muted" style="font-size:11px;margin-top:4px;">Под этим именем заводится кабинет и оно попадёт в акт и эфирную справку.
          Если рекламодатель с таким именем уже есть в медиатеке — учётка привяжется к нему, а не создаст дубль.
          Оставите пустым — возьмём фамилию и имя из учётной записи.</div></div>
      </div>
      <button class="btn primary" data-action="settings-create-user">Добавить пользователя</button>`;
  } else {
    usersBlock = `<div class="sec">Пользователи</div><div class="muted" style="font-size:12px;">Управление пользователями доступно только супер-админу. Ваша роль: ${esc(roleLabel(ME.role))}.</div>`;
  }

  view.innerHTML = `
    <div style="max-width:640px;">
      <div class="sec" style="margin-top:0;">Текущий пользователь</div>
      <div class="cell" style="display:flex;align-items:center;gap:10px;">
        <span class="dot" style="background:var(--accent);"></span>
        <div style="flex:1;">
          <div style="font-weight:500;">${esc(ME.username || '')}</div>
          <div class="muted" style="font-size:12px;">${esc(roleLabel(ME.role))}</div>
        </div>
        <button class="btn" data-action="settings-open-my-password-form">Сменить мой пароль</button>
      </div>

      <div id="mypass"></div>

      <div class="sec">Сервер</div>
      <div class="fld"><label>Адрес сервера (для агентов мини ПК)</label><input class="inp" readonly value="${location.origin}/api"></div>

      ${canWrite() ? `<div class="sec">Диагностика сервера</div>
      <div class="muted" style="font-size:12px;margin-bottom:8px;">Проверка всех служб стека (БД, Redis, Celery, MinIO, NTP, диски, миграции, бэкапы). Для каждой проблемы — подсказка, что сделать. Если панель вообще не открывается — диагностику с хоста делает <code>selfheal.sh</code> (см. runbook §1).</div>
      <div style="margin-bottom:10px;"><button class="btn primary" data-action="settings-run-selfcheck">&#9889; Запустить проверку</button></div>
      <div id="selfcheck-block"></div>` : ''}

      <div class="sec">Целевые версии ПО</div>
      <div class="muted" style="font-size:12px;margin-bottom:8px;">Эталонные версии для сравнения с мини ПК. Где версия не совпадает — панель пометит «требует обновления». Само обновление выполняется администратором на ПК вручную.</div>
      <div id="ver-block" class="muted">Загрузка…</div>

      ${usersBlock}

      ${isSuper() ? `<div class="sec">Реквизиты организации</div>
      <div class="muted" style="font-size:12px;margin-bottom:8px;">Ваши реквизиты как исполнителя.
      Подставляются в акт оказанных услуг и эфирную справку. Расчёты ведутся по УСН —
      в документах печатается «НДС не облагается».</div>
      <div id="company-block" class="muted">Загрузка…</div>` : ''}

      ${isSuper() ? `<div class="sec">Уведомления в MAX</div>
      <div class="muted" style="font-size:12px;margin-bottom:8px;">Сервер проверяет состояние сети раз в минуту и присылает боту MAX сообщение, когда экран уходит офлайн, заканчивается место на диске или ролик перестаёт воспроизводиться. Одно событие = одно сообщение (без спама), по возвращении экрана онлайн приходит уведомление о восстановлении.</div>
      <div id="notif-block" class="muted">Загрузка…</div>` : ''}

      <div class="sec">Журнал операций</div>
      <div id="audit-log-block" class="muted">Загрузка…</div>

      <div class="sec">Резервное копирование</div>
      <div style="margin-bottom:10px;display:flex;align-items:center;gap:12px;">
        <span class="muted" style="font-size:12px;">Авто-бэкап раз в 24 часа &middot; backup_*.sql.gz</span>
        <button class="btn" data-action="settings-create-backup" style="margin-left:auto;">&#10515; Создать бэкап сейчас</button>
      </div>
      <div id="backup-list" class="muted" style="font-size:12px;">Загрузка…</div>
    </div>`;

  if(isSuper()){
    try{
      const users = await api('/users');
      document.getElementById('users-list').innerHTML =
        '<table><tr><th>Логин</th><th>Роль</th><th>Статус</th><th>Последний вход</th><th style="text-align:right;">Действия</th></tr>' +
        users.map(u => {
          const isSuperRow = u.role === 'superadmin';
          const blocked = u.is_blocked;

          const roleSel = isSuperRow
            ? esc(roleLabel(u.role))
            : `<select class="inp" style="padding:4px 8px;font-size:12px;" data-action="settings-change-role" data-user-id="${u.id}">
                <option value="auditor"${u.role === 'auditor' ? ' selected' : ''}>Аудитор</option>
                <option value="moderator"${u.role === 'moderator' ? ' selected' : ''}>Модератор</option>
                <option value="admin"${u.role === 'admin' ? ' selected' : ''}>Админ</option>
                <option value="advertiser"${u.role === 'advertiser' ? ' selected' : ''}>Рекламодатель</option>
              </select>${u.advertiser_name ? `<div class="muted" style="font-size:11px;margin-top:3px;">кабинет: ${esc(u.advertiser_name)}</div>` : ''}`;

          const status = isSuperRow
            ? '<span class="muted">—</span>'
            : (blocked ? '<span style="color:var(--danger);">заблокирован</span>' : '<span style="color:var(--accent);">активен</span>');

          let actions = '<span class="dim">—</span>';
          if(!isSuperRow){
            actions = `<button class="btn" style="padding:4px 8px;font-size:12px;"
              data-action="settings-edit-user"
              data-user-id="${u.id}"
              data-username="${esc(u.username)}"
              data-first-name="${esc(u.first_name || '')}"
              data-last-name="${esc(u.last_name || '')}">Данные</button>

              <button class="btn" style="padding:4px 8px;font-size:12px;"
              data-action="settings-reset-pass"
              data-user-id="${u.id}"
              data-username="${esc(u.username)}">Сброс пароля</button>

              <button class="btn" style="padding:4px 8px;font-size:12px;"
              data-action="settings-toggle-block"
              data-user-id="${u.id}"
              data-block-value="${blocked ? 'false' : 'true'}">${blocked ? 'Разблокировать' : 'Заблокировать'}</button>

              <button class="btn danger" style="padding:4px 8px;font-size:12px;"
              data-action="settings-delete-user"
              data-user-id="${u.id}"
              data-username="${esc(u.username)}">Удалить</button>`;
          }

          return `<tr>
            <td>${esc(u.username)}${u.full_name ? '<div class="muted" style="font-size:11px;">' + esc(u.full_name) + '</div>' : ''}</td>
            <td>${roleSel}</td>
            <td style="font-size:12px;">${status}</td>
            <td class="muted" style="font-size:12px;">${u.last_login ? fmtServerTS(u.last_login) : '—'}</td>
            <td style="text-align:right;white-space:nowrap;">${actions}</td>
          </tr>`;
        }).join('') + '</table>';
    }catch(e){
      document.getElementById('users-list').innerHTML = '<div class="muted">Не удалось загрузить пользователей</div>';
    }
  }

  loadTargetVersions();
  loadAuditLog();
  loadBackups();
  if(isSuper()) loadNotifications();
}

async function loadNotifications(){
  const el = document.getElementById('notif-block');
  if(!el) return;
  try{
    const s = await api('/notifications/settings');
    const chk = (v) => v ? 'checked' : '';
    el.innerHTML = `
      <div class="cell">
        <label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer;margin-bottom:10px;">
          <input type="checkbox" id="nt-enabled" ${chk(s.enabled)}> Уведомления включены
        </label>
        <div class="row">
          <div class="fld" style="flex:2;"><label>Токен бота MAX ${s.max_token_set ? '(задан: ' + esc(s.max_token) + ')' : ''}</label>
            <input class="inp" id="nt-token" placeholder="${s.max_token_set ? 'оставьте пустым, чтобы не менять' : 'вставьте токен от @MasterBot'}"></div>
          <div class="fld" style="flex:1;"><label>chat_id получателя</label>
            <input class="inp" id="nt-chat" value="${esc(s.max_chat_id || '')}" placeholder="напишите боту, узнайте chat_id"></div>
        </div>
        <div class="fld"><label>Адрес API MAX</label>
          <input class="inp" id="nt-baseurl" value="${esc(s.base_url || '')}"></div>
        <div class="row">
          <div class="fld" style="flex:1;"><label>Экран офлайн дольше (мин)</label>
            <input class="inp" type="number" min="1" id="nt-offmin" value="${s.offline_minutes}"></div>
          <div class="fld" style="flex:1;"><label>Тревога, если свободно меньше (%)</label>
            <input class="inp" type="number" min="1" max="99" id="nt-diskpct" value="${s.disk_free_pct}"></div>
        </div>
        <div style="display:flex;gap:16px;flex-wrap:wrap;margin:4px 0 12px;font-size:13px;">
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;"><input type="checkbox" id="nt-offline" ${chk(s.notify_offline)}> Экран офлайн</label>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;"><input type="checkbox" id="nt-disk" ${chk(s.notify_disk)}> Мало места</label>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;"><input type="checkbox" id="nt-broken" ${chk(s.notify_broken)}> Нерабочий ролик</label>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;" title="Мини-ПК работает и играет контент, но монитор отключён от видеовыхода"><input type="checkbox" id="nt-display" ${chk(s.notify_display)}> Монитор отключён</label>
        </div>
        <div style="display:flex;gap:8px;">
          <button class="btn primary" data-action="settings-save-notifications">Сохранить</button>
          <button class="btn" data-action="settings-test-notification">📨 Отправить тест</button>
        </div>
      </div>`;
  }catch(e){
    el.innerHTML = '<div class="muted" style="font-size:12px;">Не удалось загрузить настройки уведомлений</div>';
  }
}

async function saveNotifications(){
  const body = {
    enabled: document.getElementById('nt-enabled').checked,
    max_chat_id: val('nt-chat'),
    base_url: val('nt-baseurl'),
    offline_minutes: Number(val('nt-offmin')),
    disk_free_pct: Number(val('nt-diskpct')),
    notify_offline: document.getElementById('nt-offline').checked,
    notify_disk: document.getElementById('nt-disk').checked,
    notify_broken: document.getElementById('nt-broken').checked,
    notify_display: document.getElementById('nt-display').checked,
  };
  const tok = val('nt-token');
  if(tok) body.max_token = tok;   // пусто = не менять существующий
  try{
    await api('/notifications/settings', {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    toast('Настройки уведомлений сохранены');
    loadCompany();
    loadNotifications();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function testNotification(){
  try{
    toast('Отправляем тест…');
    await api('/notifications/test', {method:'POST'});
    toast('Тестовое сообщение отправлено в MAX ✓');
  }catch(e){ toast('Не доставлено: ' + e.message); }
}

async function runSelfcheck(){
  const el = document.getElementById('selfcheck-block');
  if(!el) return;
  el.innerHTML = '<div class="muted" style="font-size:12px;">Проверяю… (до 10 секунд: опрашиваются все службы)</div>';
  try{
    const r = await api('/system/selfcheck');
    const ico = s => s==='ok' ? '&#10003;' : (s==='warn' ? '&#9888;' : '&#10007;');
    const col = s => s==='ok' ? 'var(--accent)' : (s==='warn' ? 'var(--c-nike)' : 'var(--danger)');
    const head = r.status==='ok'
      ? 'Все проверки пройдены'
      : (r.status==='warn' ? 'Есть предупреждения' : 'Есть проблемы — нужны действия');
    el.innerHTML = `
      <div class="cell" style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
        <span style="color:${col(r.status)};font-weight:600;">${ico(r.status)} ${head}</span>
        <span class="muted" style="font-size:11px;margin-left:auto;">${new Date(r.checked_at).toLocaleTimeString('ru-RU')}</span>
      </div>
      ${r.checks.map(c => `
        <div class="cell" style="margin-bottom:6px;">
          <div style="display:flex;align-items:center;gap:8px;">
            <span style="color:${col(c.status)};min-width:14px;">${ico(c.status)}</span>
            <span style="font-weight:500;">${esc(c.title)}</span>
            <span class="muted" style="font-size:12px;margin-left:auto;">${esc(c.detail||'')}</span>
          </div>
          ${c.action ? `<div style="font-size:12px;color:var(--txt2);margin:6px 0 0 22px;">&#8627; ${esc(c.action)}</div>` : ''}
        </div>`).join('')}`;
  }catch(e){
    el.innerHTML = `<div style="color:var(--danger);font-size:12px;">Не удалось выполнить проверку: ${esc(e.message||String(e))}. Если панель работает, а проверка падает — смотрите логи API: <code>docker compose logs --tail=50 api</code></div>`;
  }
}

function initSettingsViewActions(){
  if(window.__settingsViewActionsInitialized) return;
  window.__settingsViewActionsInitialized = true;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('settings-')) return;

    switch(action){
      case 'settings-create-user':
        return createUser();

      case 'settings-save-company':
        return saveCompany();

      case 'settings-open-my-password-form':
        return myPasswordForm();

      case 'settings-create-backup':
        return createBackup();

      case 'settings-run-selfcheck':
        return runSelfcheck();

      case 'settings-save-notifications':
        return saveNotifications();

      case 'settings-test-notification':
        return testNotification();

      case 'settings-save-target-versions':
        return saveTargetVersions();

      case 'settings-save-my-password':
        return saveMyPassword();

      case 'settings-cancel-my-password':
        document.getElementById('mypass').innerHTML = '';
        return;

      case 'settings-close-edit-user-modal':
        document.getElementById('edit-user-modal')?.remove();
        return;

      case 'settings-save-edit-user': {
        const id = Number(el.dataset.userId);
        return saveEditUser(id);
      }

      case 'settings-edit-user': {
        const id = Number(el.dataset.userId);
        const username = el.dataset.username || '';
        const firstName = el.dataset.firstName || '';
        const lastName = el.dataset.lastName || '';
        return editUser(id, username, firstName, lastName);
      }

      case 'settings-reset-pass': {
        const id = Number(el.dataset.userId);
        const username = el.dataset.username || '';
        return resetPass(id, username);
      }

      case 'settings-toggle-block': {
        const id = Number(el.dataset.userId);
        const block = el.dataset.blockValue === 'true';
        return toggleBlock(id, block);
      }

      case 'settings-delete-user': {
        const id = Number(el.dataset.userId);
        const username = el.dataset.username || '';
        return delUser(id, username);
      }

      case 'settings-delete-backup': {
        const id = Number(el.dataset.backupId);
        const filename = el.dataset.filename || '';
        return deleteBackup(id, filename);
      }
    }
  });

  document.addEventListener('change', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('settings-')) return;

    switch(action){
      case 'settings-change-role': {
        const id = Number(el.dataset.userId);
        // Смена роли на «рекламодатель» требует наименования кабинета:
        // спрашиваем сразу, иначе кабинет назовётся логином.
        if(el.value === 'advertiser'){
          const name = prompt('Наименование рекламодателя для кабинета:');
          if(name === null){ viewSettings(); return; }   // отменили — вернуть прежнюю роль
          return changeRole(id, el.value, name);
        }
        return changeRole(id, el.value);
      }
      case 'settings-role-changed': {
        // Поле наименования нужно только рекламодателю
        const row = document.getElementById('nu-adv-row');
        if(row) row.style.display = (el.value === 'advertiser') ? '' : 'none';
        return;
      }
    }
  });
}

async function loadAuditLog(){
  const el = document.getElementById('audit-log-block');
  if(!el) return;

  try{
    const rows = await api('/audit?limit=20');
    if(!rows.length){
      el.innerHTML = '<div class="muted" style="font-size:12px;">Нет записей</div>';
      return;
    }

    el.innerHTML = '<table><tr><th>Тип</th><th>Операция</th><th>Детали</th><th>Автор</th><th>Время</th></tr>' +
      rows.map(r => `<tr>
        <td class="muted" style="font-size:11px;">${esc(r.event_type || '')}</td>
        <td>${esc(r.title || '')}</td>
        <td class="muted" style="font-size:11px;">${esc(r.detail || '—')}</td>
        <td class="muted" style="font-size:11px;">${esc(r.actor || '—')}</td>
        <td class="muted" style="font-size:11px;">${r.created_at ? fmtServerTS(r.created_at) : '—'}</td>
      </tr>`).join('') + '</table>';
  }catch(e){
    el.innerHTML = '<div class="muted" style="font-size:12px;">Не удалось загрузить журнал</div>';
  }
}

async function loadTargetVersions(){
  const el = document.getElementById('ver-block');
  if(!el) return;

  try{
    const t = await api('/versions/target');
    const editable = canWrite();

    el.innerHTML = `
      <div class="row">
        <div class="fld" style="flex:1;"><label>Целевая версия ОС (Astra)</label><input class="inp" id="tgt-os" value="${esc(t.os_version || '')}" placeholder="например, 1.7" ${editable ? '' : 'readonly'}></div>
        <div class="fld" style="flex:1;"><label>Целевая версия плеера (mpv)</label><input class="inp" id="tgt-vlc" value="${esc(t.vlc_version || '')}" placeholder="например, 0.35.0" ${editable ? '' : 'readonly'}></div>
      </div>
      ${editable ? '<button class="btn primary" data-action="settings-save-target-versions">Сохранить целевые версии</button>' : ''}
      ${t.updated_at ? `<div class="muted" style="font-size:11px;margin-top:6px;">Обновлено: ${fmtServerTS(t.updated_at)}${t.updated_by ? ' · ' + esc(t.updated_by) : ''}</div>` : ''}`;
  }catch(e){
    el.innerHTML = '<div class="muted" style="font-size:12px;">Не удалось загрузить целевые версии</div>';
  }
}

async function saveTargetVersions(){
  const os = val('tgt-os');
  const vlc = val('tgt-vlc');

  try{
    await api('/versions/target', {
      method:'POST',
      headers:{ 'Content-Type':'application/json' },
      body:JSON.stringify({ os_version: os, vlc_version: vlc })
    });
    toast('Целевые версии сохранены');
    loadTargetVersions();
  }catch(e){
    toast('Ошибка: ' + e.message);
  }
}

function myPasswordForm(){
  document.getElementById('mypass').innerHTML = `
    <div class="cell" style="margin-top:8px;">
      <div class="row">
        <div class="fld" style="flex:1;"><label>Текущий пароль</label><input class="inp" id="mp-old" type="password"></div>
        <div class="fld" style="flex:1;"><label>Новый пароль</label><input class="inp" id="mp-new" type="password" placeholder="мин. 6 символов"></div>
      </div>
      <button class="btn primary" data-action="settings-save-my-password">Сохранить новый пароль</button>
      <button class="btn" data-action="settings-cancel-my-password">Отмена</button>
    </div>`;
}

async function saveMyPassword(){
  const oldp = val('mp-old');
  const newp = val('mp-new');

  if(!oldp || !newp){
    toast('Заполните оба поля');
    return;
  }

  try{
    await api('/me/password', {
      method:'POST',
      headers:{ 'Content-Type':'application/json' },
      body:JSON.stringify({ old_password: oldp, new_password: newp })
    });
    toast('Пароль изменён');
    document.getElementById('mypass').innerHTML = '';
  }catch(e){
    toast('Ошибка: ' + e.message);
  }
}

async function createUser(){
  const username = val('nu-login');
  const password = val('nu-pass');
  const role = document.getElementById('nu-role').value;
  const first_name = val('nu-firstname') || '';
  const last_name = val('nu-lastname') || '';
  const advertiser_name = val('nu-advname') || '';

  if(!username || !password){
    toast('Введите логин и пароль');
    return;
  }

  try{
    await api('/users', {
      method:'POST',
      headers:{ 'Content-Type':'application/json' },
      body:JSON.stringify({ username, password, role, first_name, last_name, advertiser_name })
    });
    toast('Пользователь добавлен');
    viewSettings();
  }catch(e){
    toast('Ошибка: ' + e.message);
  }
}

async function changeRole(id, role, advertiser_name){
  try{
    await api('/users/' + id + '/role', {
      method:'PATCH',
      headers:{ 'Content-Type':'application/json' },
      body:JSON.stringify({ role, advertiser_name: advertiser_name || '' })
    });
    toast(role === 'advertiser' ? 'Роль изменена, кабинет привязан' : 'Роль изменена');
    if(role === 'advertiser') viewSettings();   // показать привязанный кабинет
  }catch(e){
    toast('Ошибка: ' + e.message);
    viewSettings();
  }
}

async function toggleBlock(id, block){
  block = (block === true || block === 'true');

  try{
    await api('/users/' + id + '/block', {
      method:'PATCH',
      headers:{ 'Content-Type':'application/json' },
      body:JSON.stringify({ blocked: block })
    });
    toast(block ? 'Пользователь заблокирован' : 'Пользователь разблокирован');
    viewSettings();
  }catch(e){
    toast('Ошибка: ' + e.message);
  }
}

async function resetPass(id, name){
  const np = prompt('Новый пароль для «' + name + '» (мин. 6 символов):');
  if(np === null) return;
  if(np.length < 6){
    toast('Слишком короткий пароль');
    return;
  }

  try{
    await api('/users/' + id + '/reset-password', {
      method:'PATCH',
      headers:{ 'Content-Type':'application/json' },
      body:JSON.stringify({ new_password: np })
    });
    toast('Пароль сброшен');
  }catch(e){
    toast('Ошибка: ' + e.message);
  }
}

function editUser(id, username, first_name, last_name){
  const ex = document.getElementById('edit-user-modal');
  if(ex) ex.remove();

  const m = document.createElement('div');
  m.id = 'edit-user-modal';
  m.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:200;display:flex;align-items:center;justify-content:center;';
  m.innerHTML = `<div style="background:var(--panel);border:0.5px solid var(--border2);border-radius:12px;padding:24px;width:380px;">
    <div style="font-size:15px;font-weight:600;margin-bottom:16px;">Данные пользователя</div>
    <div class="fld" style="margin-bottom:10px;"><label>Фамилия</label><input class="inp" id="eu-lastname" value="${esc(last_name || '')}" placeholder="Иванов"></div>
    <div class="fld" style="margin-bottom:10px;"><label>Имя</label><input class="inp" id="eu-firstname" value="${esc(first_name || '')}" placeholder="Иван"></div>
    <div class="fld" style="margin-bottom:16px;"><label>Логин</label><input class="inp" id="eu-login" value="${esc(username || '')}"></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;">
      <button class="btn" data-action="settings-close-edit-user-modal">Отмена</button>
      <button class="btn primary" data-action="settings-save-edit-user" data-user-id="${id}">Сохранить</button>
    </div>
  </div>`;

  document.body.appendChild(m);
  m.addEventListener('click', e => {
    if(e.target === m) m.remove();
  });
}

async function saveEditUser(id){
  const nu = val('eu-login');
  const fn = val('eu-firstname');
  const ln = val('eu-lastname');

  if(!nu){
    toast('Логин не может быть пустым');
    return;
  }

  try{
    await api('/users/' + id, {
      method:'PATCH',
      headers:{ 'Content-Type':'application/json' },
      body:JSON.stringify({ username: nu, first_name: fn, last_name: ln })
    });
    toast('Данные обновлены');
    document.getElementById('edit-user-modal').remove();
    viewSettings();
  }catch(e){
    toast('Ошибка: ' + e.message);
  }
}

async function delUser(id, name){
  if(!confirm('Удалить пользователя «' + name + '»?')) return;

  try{
    await api('/users/' + id, { method:'DELETE' });
    toast('Пользователь удалён');
    viewSettings();
  }catch(e){
    toast('Ошибка: ' + e.message);
  }
}

//=============================================================================
// РЕЗЕРВНОЕ КОПИРОВАНИЕ
//=============================================================================

async function loadBackups(){
  const el = document.getElementById('backup-list');
  if(!el) return;

  try{
    const list = await api('/backups');
    if(!list || !list.length){
      el.innerHTML = '<div class="muted">Бэкапов нет</div>';
      return;
    }

    el.innerHTML = '<table><tr><th>Файл</th><th>Дата</th><th>Размер</th><th style="text-align:right;">Действия</th></tr>' +
      list.map(b => `<tr>
        <td style="font-family:monospace;font-size:12px;">${esc(b.filename)}</td>
        <td class="muted" style="font-size:12px;white-space:nowrap;">${b.created_at ? fmtServerTS(b.created_at) : '—'}</td>
        <td class="muted" style="font-size:12px;">${b.size_bytes ? Math.round(b.size_bytes / 1024 / 1024 * 10) / 10 + ' МБ' : '—'}</td>
        <td style="text-align:right;white-space:nowrap;">
          <a class="btn" style="padding:4px 8px;font-size:12px;text-decoration:none;" href="/api/backups/${b.id}/download" download>&#10515; Скачать</a>
          <button class="btn danger" style="padding:4px 8px;font-size:12px;"
            data-action="settings-delete-backup"
            data-backup-id="${b.id}"
            data-filename="${esc(b.filename)}">Удалить</button>
        </td>
      </tr>`).join('') + '</table>';
  }catch(e){
    el.innerHTML = '<div class="muted">Ошибка загрузки бэкапов</div>';
  }
}

async function createBackup(){
  try{
    toast('Создаётся бэкап…');
    await api('/backups/create', { method:'POST' });
    toast('Бэкап создан');
    loadBackups();
  }catch(e){
    toast('Ошибка: ' + e.message);
  }
}

async function deleteBackup(id, filename){
  if(!confirm('Удалить бэкап «' + filename + '»?')) return;

  try{
    await api('/backups/' + id, { method:'DELETE' });
    toast('Бэкап удалён');
    loadBackups();
  }catch(e){
    toast('Ошибка: ' + e.message);
  }
}

window.Signage = window.Signage || {};
window.Signage.viewSettings = viewSettings;
window.Signage.loadAuditLog = loadAuditLog;
window.Signage.loadTargetVersions = loadTargetVersions;
window.Signage.saveTargetVersions = saveTargetVersions;
window.Signage.myPasswordForm = myPasswordForm;
window.Signage.saveMyPassword = saveMyPassword;
window.Signage.createUser = createUser;
window.Signage.changeRole = changeRole;
window.Signage.toggleBlock = toggleBlock;
window.Signage.resetPass = resetPass;
window.Signage.editUser = editUser;
window.Signage.saveEditUser = saveEditUser;
window.Signage.delUser = delUser;
window.Signage.loadBackups = loadBackups;
window.Signage.createBackup = createBackup;
window.Signage.deleteBackup = deleteBackup;
window.Signage.loadNotifications = loadNotifications;
window.Signage.saveNotifications = saveNotifications;
window.Signage.testNotification = testNotification;

initSettingsViewActions();



// ─── Реквизиты организации (исполнителя) ──────────────────────────────────
// Нужны для акта и эфирной справки: без них документы сформируются с пустой
// шапкой, поэтому сервер прямо просит их заполнить перед первой генерацией.
async function loadCompany(){
  const el = document.getElementById('company-block');
  if(!el) return;
  try{
    const c = await api('/company/settings');
    const f = (id, label, val, ph) =>
      `<div class="fld" style="flex:1;"><label>${label}</label>
        <input class="inp" id="co-${id}" value="${esc(val || '')}" placeholder="${ph || ''}"></div>`;
    el.innerHTML = `<div class="cell">
      <div class="row">
        ${f('legal_name', 'Полное наименование', c.legal_name, 'ООО «Цифровые Экраны»')}
        ${f('short_name', 'Краткое', c.short_name, 'ООО «ЦЭ»')}
      </div>
      <div class="row">
        ${f('inn', 'ИНН', c.inn)}${f('kpp', 'КПП', c.kpp)}${f('ogrn', 'ОГРН', c.ogrn)}
      </div>
      ${f('legal_address', 'Юридический адрес', c.legal_address)}
      <div class="row">
        ${f('bank_name', 'Банк', c.bank_name)}${f('bank_bik', 'БИК', c.bank_bik)}
      </div>
      <div class="row">
        ${f('bank_account', 'Расчётный счёт', c.bank_account)}${f('corr_account', 'Корр. счёт', c.corr_account)}
      </div>
      <div class="row">
        ${f('director_post', 'Должность подписанта', c.director_post, 'Генеральный директор')}
        ${f('director_name', 'ФИО подписанта', c.director_name, 'Иванов И.И.')}
      </div>
      <div class="row">
        ${f('phone', 'Телефон', c.phone)}${f('email', 'E-mail', c.email)}
      </div>
      <div class="muted" style="font-size:11px;margin:4px 0 8px;">Налоговый режим: УСН, НДС не начисляется.</div>
      <button class="btn primary" data-action="settings-save-company">Сохранить реквизиты</button>
    </div>`;
  }catch(e){
    el.innerHTML = '<div class="muted" style="font-size:12px;">Не удалось загрузить реквизиты</div>';
  }
}

async function saveCompany(){
  const ids = ['legal_name','short_name','inn','kpp','ogrn','legal_address','bank_name',
               'bank_bik','bank_account','corr_account','director_post','director_name','phone','email'];
  const body = {};
  ids.forEach(i => body[i] = val('co-' + i));
  try{
    await api('/company/settings', {method:'PATCH', headers:{'Content-Type':'application/json'},
                                    body: JSON.stringify(body)});
    toast('Реквизиты сохранены');
  }catch(e){ toast('Ошибка: ' + e.message); }
}
window.Signage.saveCompany = saveCompany;
