async function viewOta(){
  if(!isSuper() && !canWrite()){
    document.getElementById('view').innerHTML='<div class="empty">Требуется роль admin или superadmin.</div>'; return;
  }
  const view=document.getElementById('view');
  view.innerHTML='<div class="empty">Загрузка…</div>';
  try{
    const [files, history] = await Promise.all([
      api('/agent/files').catch(()=>[]),
      api('/agent/updates').catch(()=>[])
    ]);
    OTA_FILES_CACHE = files;

    // ── Загруженные файлы агента ──────────────────────────────────────────
    let filesHtml = '';
    if(files.length){
      filesHtml = '<table><tr><th>Файл</th><th>Размер</th><th>MD5</th><th>Дата</th></tr>'+
        files.map(f=>{
          const dt = f.modified ? new Date(f.modified*1000).toLocaleString('ru-RU') : '—';
          const sz = f.size>1024 ? (f.size/1024).toFixed(0)+' КБ' : f.size+' Б';
          return `<tr><td style="font-family:monospace;">${esc(f.name)}</td><td>${sz}</td>
            <td style="font-family:monospace;font-size:10px;color:var(--muted);">${esc(f.md5||'—')}</td>
            <td style="font-size:11px;color:var(--muted);">${dt}</td></tr>`;
        }).join('')+'</table>';
    } else {
      filesHtml = '<div class="muted" style="font-size:12px;">Файлов агента ещё не загружено.</div>';
    }

    // ── История обновлений ─────────────────────────────────────────────────
    let histHtml = '';
    if(history.length){
      histHtml = '<table><tr><th>Версия</th><th>Экранов</th><th>Файлы</th><th>Автор</th><th>Дата</th></tr>'+
        history.map(u=>{
          const dt = u.created_at ? fmtServerTS(u.created_at) : '—';
          let filesList = '—';
          try{ const ff = typeof u.files==='string' ? JSON.parse(u.files) : (u.files||[]);
            filesList = ff.map(f=>f.name||f).join(', ') || '—'; }catch(e){}
          return `<tr>
            <td style="font-weight:600;">${esc(u.version||'—')}</td>
            <td>${u.screens_total||'—'}</td>
            <td style="font-size:11px;font-family:monospace;">${esc(filesList)}</td>
            <td style="font-size:11px;">${esc(u.created_by||'—')}</td>
            <td style="font-size:11px;color:var(--muted);">${dt}</td></tr>`;
        }).join('')+'</table>';
    } else {
      histHtml = '<div class="muted" style="font-size:12px;">История обновлений пуста.</div>';
    }

    // ── Форма отправки обновления ─────────────────────────────────────────
    const allowedFiles = ['ds_agent.py','ds_player.py','ds_sync.py','ds_heartbeat.py','ds_downloader.py','ds_cleanup.py','ds_ws_client.py'];
    const fileCheckboxes = allowedFiles.map(fn=>{
      const exists = files.find(f=>f.name===fn);
      return `<label style="display:inline-flex;align-items:center;gap:5px;margin-right:12px;margin-bottom:5px;font-size:12px;${exists?'':'opacity:0.4'}">
        <input type="checkbox" class="ota-file-chk" value="${fn}" ${exists?'checked':'disabled'}> ${fn}</label>`;
    }).join('');

    view.innerHTML=`
      <div style="max-width:760px;">
        <div class="sec" style="margin-top:0;">Файлы агента на сервере</div>
        <div style="margin-bottom:10px;">${filesHtml}</div>
        ${canWrite()?`
        <div class="sec">Загрузить новый файл агента</div>
        <div class="muted" style="font-size:12px;margin-bottom:8px;">Допустимые файлы: ${allowedFiles.join(', ')}</div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
          <input class="inp" type="file" id="ota-file-input" accept=".py" style="flex:1;min-width:200px;">
          <button class="btn primary" data-action="ota-upload">Загрузить</button>

        </div>
        <div id="ota-upload-result" style="margin-top:8px;font-size:12px;"></div>

        <div class="sec" style="margin-top:18px;">Отправить обновление на экраны</div>
        <div class="muted" style="font-size:12px;margin-bottom:8px;">Выберите файлы и экраны. Агент проверит MD5 и синтаксис перед применением.</div>
        <div style="margin-bottom:10px;"><div style="font-size:12px;font-weight:600;margin-bottom:5px;">Файлы для обновления:</div>${fileCheckboxes}</div>
        <div class="row" style="flex-wrap:wrap;gap:8px;">
          <div class="fld" style="flex:1;min-width:160px;"><label>Версия</label><input class="inp" id="ota-version" placeholder="1.5.0" value=""></div>
          <div class="fld" style="flex:2;min-width:240px;"><label>Описание изменений (необязательно)</label><input class="inp" id="ota-changelog" placeholder="Исправление WS-переподключения"></div>
        </div>
        <div class="fld"><label>Экраны (пусто = все)</label>
          <div id="ota-screens-list" class="muted" style="font-size:12px;">Загрузка…</div>
        </div>
        <div style="display:flex;gap:8px;margin-top:10px;">
          <button class="btn primary" data-action="ota-push">▶ Отправить обновление</button>

          <button class="btn" data-action="ota-refresh">↺ Обновить</button>

        </div>
        <div id="ota-push-result" style="margin-top:8px;font-size:12px;"></div>
        `:''}

        <div class="sec" style="margin-top:20px;">История отправленных обновлений</div>
        ${histHtml}
      </div>`;

    // Загружаем список экранов для выбора
    if(canWrite()){
      try{
        const screens = await api('/minipc');
        const screensHtml = screens.length
          ? screens.map(s=>`<label style="display:inline-flex;align-items:center;gap:5px;margin-right:12px;margin-bottom:4px;font-size:12px;">
              <input type="checkbox" class="ota-screen-chk" value="${s.id}"> ${esc(s.name)} <span style="color:${s.status==='online'?'var(--accent)':'var(--danger)'};font-size:10px;">(${s.status==='online'?'онлайн':'офлайн'})</span>
            </label>`).join('')
          : '<span class="muted">Экранов нет</span>';
        const el = document.getElementById('ota-screens-list');
        if(el) el.innerHTML='<div style="margin-top:4px;">'+screensHtml+'</div><div style="font-size:11px;color:var(--muted);margin-top:4px;">Не выбрано — обновление отправится всем экранам</div>';
      }catch(e){}
    }
  }catch(e){ view.innerHTML='<div class="empty">Ошибка загрузки OTA: '+esc(e.message)+'</div>'; }
}

async function otaUpload(){
  const inp = document.getElementById('ota-file-input');
  const res = document.getElementById('ota-upload-result');
  if(!inp||!inp.files.length){ toast('Выберите файл'); return; }
  const file = inp.files[0];
  const allowed = ['ds_agent.py','ds_player.py','ds_sync.py','ds_heartbeat.py','ds_downloader.py','ds_cleanup.py','ds_ws_client.py'];
  if(!allowed.includes(file.name)){ toast('Недопустимый файл: '+file.name); return; }
  res.textContent = 'Загрузка…';
  try{
    const form = new FormData();
    form.append('file', file, file.name);
    const resp = await fetch(API+'/agent/files/upload', {
      method:'POST', headers:{'Authorization':'Bearer '+TOKEN}, body:form
    });
    if(!resp.ok){ const e=await resp.json().catch(()=>({detail:'ошибка'})); throw new Error(e.detail||resp.status); }
    const data = await resp.json();
    res.innerHTML=`<span style="color:var(--accent);">✓ Загружен: ${esc(data.name)} (${(data.size/1024).toFixed(0)} КБ, MD5: ${esc(data.md5)})</span>`;
    toast('Файл загружен: '+data.name);
    setTimeout(viewOta, 600);
  }catch(e){ res.innerHTML=`<span style="color:var(--danger);">Ошибка: ${esc(e.message)}</span>`; }
}

async function otaPush(){
  const res = document.getElementById('ota-push-result');
  const version = (document.getElementById('ota-version')||{}).value||'auto';
  const changelog = (document.getElementById('ota-changelog')||{}).value||'';
  const files = [...document.querySelectorAll('.ota-file-chk:checked:not(:disabled)')].map(c=>c.value);
  const screenIds = [...document.querySelectorAll('.ota-screen-chk:checked')].map(c=>Number(c.value));
  if(!files.length){ toast('Выберите хотя бы один файл'); return; }
  if(!version||version==='auto'||version.trim()===''){
    if(!confirm('Версия не указана. Отправить с автоматической версией?')) return;
  }
  res.textContent = 'Отправка команды…';
  try{
    const data = await api('/agent/push', {method:'POST', body:JSON.stringify({
      screen_ids: screenIds,
      version: version.trim()||'auto',
      changelog: changelog,
      files: files
    })});
    res.innerHTML=`<span style="color:var(--accent);">✓ Команда отправлена на ${data.screens_count} экран(ов). Версия: ${esc(data.version)}. Файлов: ${data.files.length}.</span>`;
    toast('OTA-обновление отправлено');
    setTimeout(viewOta, 1500);
  }catch(e){ res.innerHTML=`<span style="color:var(--danger);">Ошибка: ${esc(e.message)}</span>`; }
}
function initOtaViewActions(){
  if(window.__otaViewActionsInitialized) return;
  window.__otaViewActionsInitialized = true;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('ota-')) return;

    switch(action){
      case 'ota-upload':
        return Signage.otaUpload();

      case 'ota-push':
        return Signage.otaPush();

      case 'ota-refresh':
        return Signage.viewOta();
    }
  });
}

window.Signage = window.Signage || {};
window.Signage.viewOta = viewOta;
window.Signage.otaUpload = otaUpload;
window.Signage.otaPush = otaPush;
initOtaViewActions();
