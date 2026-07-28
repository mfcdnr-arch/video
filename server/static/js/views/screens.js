// Копирование в буфер обмена. navigator.clipboard доступен только в защищённом
// контексте (HTTPS или localhost); по HTTP на IP он undefined — поэтому запасной
// путь через execCommand с временным textarea.
function copyToClipboard(text){
  if(navigator.clipboard && window.isSecureContext){
    navigator.clipboard.writeText(text)
      .then(()=>toast('Токен скопирован'))
      .catch(()=>fallbackCopy(text));
    return;
  }
  fallbackCopy(text);
}
function fallbackCopy(text){
  try{
    const ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly','');
    ta.style.position='fixed'; ta.style.top='-1000px'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    toast(ok ? 'Токен скопирован' : 'Выделите токен и скопируйте вручную (Ctrl+C)');
  }catch(e){
    toast('Скопируйте токен вручную (выделите и Ctrl+C)');
  }
}

function initScreensViewActions(){
  if(window.__screensViewActionsBound) return;
  window.__screensViewActionsBound = true;

  document.addEventListener('click', async (e)=>{
    const btn = e.target.closest('[data-action]');
    if(!btn) return;

    try{
      switch(btn.dataset.action){
        case 'switch-screens-tab':
          return viewScreens(btn.dataset.tab || 'screens');

        case 'open-group-add-form':
          return groupAddForm();

        case 'open-screen-add-form':
          return screenAddForm();

        case 'play-screen':
          return playScreen(Number(btn.dataset.screenId), btn.dataset.screenName || '');

        case 'stop-screen':
          return stopScreen(Number(btn.dataset.screenId), btn.dataset.screenName || '');

        case 'restart-agent':
          return restartAgent(Number(btn.dataset.screenId), btn.dataset.screenName || '');

        case 'screen-offline-diagnostics':
          return toast('Экран офлайн — проверьте питание и сеть');

        case 'screen-collect-diag':
          return collectScreenDiag(Number(btn.dataset.screenId));

        case 'screen-refresh-diag':
          return showScreenDiagList(Number(btn.dataset.screenId));

        case 'delete-screen':
          return delScreen(Number(btn.dataset.screenId), btn.dataset.screenName || '');

        case 'remove-from-group':
          return removeFromGroup(
            Number(btn.dataset.groupId),
            Number(btn.dataset.screenId),
            btn.dataset.screenName || ''
          );

        case 'open-sync-start-form':
          return syncStartForm(Number(btn.dataset.groupId), btn.dataset.groupName || '');

        case 'open-add-to-group-form':
          return addToGroupForm(
            Number(btn.dataset.groupId),
            btn.dataset.groupName || '',
            JSON.parse(btn.dataset.availableScreens || '[]')
          );

        case 'delete-group':
          return deleteGroup(Number(btn.dataset.groupId), btn.dataset.groupName || '');

        case 'confirm-add-to-group':
          return confirmAddToGroup(Number(btn.dataset.groupId));

        case 'close-group-modal':
          document.getElementById('modal-grp')?.remove();
          return;

        case 'submit-create-group':
          return createGroup();

        case 'cancel-create-group':
          return viewScreens('groups');

        case 'submit-sync-start':
          return doSyncStart(Number(btn.dataset.groupId));

        case 'close-sync-modal':
          document.getElementById('modal-sync')?.remove();
          return;

        case 'submit-create-screen':
          return createScreen();

        case 'cancel-create-screen':
          return nav('screens');

        case 'copy-screen-token': {
          const token = btn.dataset.token || '';
          copyToClipboard(token);
          return;
        }

        case 'open-screen-settings':
          return openScreenSettings(btn.dataset);

        case 'save-screen-settings':
          return saveScreenSettings(Number(btn.dataset.screenId));

        case 'close-screen-settings':
          document.getElementById('scr-settings-form-' + btn.dataset.screenId)?.remove();
          return;
      }
    }catch(err){
      toast('Ошибка: ' + err.message);
    }
  });

  document.addEventListener('change', (e)=>{
    const el = e.target.closest('[data-action]');
    if(!el) return;

    if(el.dataset.action === 'assign-group'){
      assignGroup(Number(el.dataset.screenId), el.value);
    }
  });
}

function openScreenSettings(d){
  const id = Number(d.screenId);
  const box = document.getElementById('scr-settings-' + id);
  if(!box) return;
  if(document.getElementById('scr-settings-form-' + id)){
    document.getElementById('scr-settings-form-' + id).remove();
    return;
  }
  box.innerHTML = `<div id="scr-settings-form-${id}" style="margin-top:9px;border-top:0.5px solid var(--border2);padding-top:9px;">
    <div style="font-size:11px;color:var(--muted);margin-bottom:6px;">Тип площадки (реклама алкоголя — только «Магазин с алколицензией», 38-ФЗ ст. 21)</div>
    <select class="inp" id="venue-${id}" style="width:auto;padding:4px 6px;margin-bottom:8px;">
      <option value="other" ${(d.venue||'other')==='other'?'selected':''}>Прочее</option>
      <option value="store_alcohol" ${d.venue==='store_alcohol'?'selected':''}>Магазин с алколицензией</option>
      <option value="store" ${d.venue==='store'?'selected':''}>Магазин</option>
      <option value="mall" ${d.venue==='mall'?'selected':''}>Торговый центр</option>
      <option value="office" ${d.venue==='office'?'selected':''}>Офис</option>
    </select>
    <div style="font-size:11px;color:var(--muted);margin-bottom:6px;">Питание монитора (МСК, пусто = всегда включён)</div>
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px;">
      <label style="font-size:11px;">вкл</label><input class="inp" type="time" id="pon-${id}" value="${d.pon||''}" style="width:auto;padding:4px 6px;">
      <label style="font-size:11px;">выкл</label><input class="inp" type="time" id="poff-${id}" value="${d.poff||''}" style="width:auto;padding:4px 6px;">
    </div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:6px;">Громкость 0–100 (ночное окно пусто = всегда дневная)</div>
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:8px;">
      <label style="font-size:11px;">день</label><input class="inp" type="number" min="0" max="100" id="vday-${id}" value="${d.vday||100}" style="width:60px;padding:4px 6px;">
      <label style="font-size:11px;">ночь</label><input class="inp" type="number" min="0" max="100" id="vnight-${id}" value="${d.vnight||100}" style="width:60px;padding:4px 6px;">
      <label style="font-size:11px;">с</label><input class="inp" type="time" id="nfrom-${id}" value="${d.nfrom||''}" style="width:auto;padding:4px 6px;">
      <label style="font-size:11px;">до</label><input class="inp" type="time" id="nto-${id}" value="${d.nto||''}" style="width:auto;padding:4px 6px;">
    </div>
    <div style="display:flex;gap:6px;">
      <button class="btn primary" data-action="save-screen-settings" data-screen-id="${id}">Сохранить</button>
      <button class="btn" data-action="close-screen-settings" data-screen-id="${id}">Отмена</button>
    </div>
  </div>`;
}

async function saveScreenSettings(id){
  const pon = val('pon-'+id), poff = val('poff-'+id);
  if((pon && !poff) || (!pon && poff)){ toast('Окно питания: заполните оба времени или очистите оба'); return; }
  const nfrom = val('nfrom-'+id), nto = val('nto-'+id);
  if((nfrom && !nto) || (!nfrom && nto)){ toast('Ночное окно: заполните оба времени или очистите оба'); return; }
  const body = {
    venue_type: val('venue-'+id) || 'other',
    power_on_time: pon || null, power_off_time: poff || null,
    volume_day: Number(val('vday-'+id)), volume_night: Number(val('vnight-'+id)),
    night_from: nfrom || null, night_to: nto || null,
  };
  try{
    await api('/minipc/' + id + '/settings', {method:'PATCH',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    toast('Настройки сохранены — агент применит при следующем опросе');
    viewScreens('screens');
  }catch(e){ toast('Ошибка: ' + e.message); }
}

initScreensViewActions();

async function viewScreens(tab){
  tab = tab || 'screens';
  const view=document.getElementById('view');
  const topright = document.getElementById('topright');

  const tabsHtml = `<div style="display:flex;gap:0;margin-bottom:16px;border-bottom:1px solid var(--border);">
    <button data-action="switch-screens-tab" data-tab="screens" style="padding:7px 20px;background:none;border:none;border-bottom:2px solid ${tab==='screens'?'var(--txt)':'transparent'};color:${tab==='screens'?'var(--txt)':'var(--muted)'};cursor:pointer;font-size:13px;font-weight:${tab==='screens'?'600':'400'};">Экраны</button>
    <button data-action="switch-screens-tab" data-tab="groups" style="padding:7px 20px;background:none;border:none;border-bottom:2px solid ${tab==='groups'?'var(--txt)':'transparent'};color:${tab==='groups'?'var(--txt)':'var(--muted)'};cursor:pointer;font-size:13px;font-weight:${tab==='groups'?'600':'400'};">Группы</button>
  </div>`;

  if(tab === 'groups'){
    topright.innerHTML = canWrite()
      ? `<button class="btn primary" data-action="open-group-add-form">+ Создать группу</button>`
      : '';
    await viewGroups(view, tabsHtml);
    return;
  }

  topright.innerHTML = canWrite()
    ? `<button class="btn primary" data-action="open-screen-add-form">+ Добавить экран</button>`
    : '';

  try{
    const [list, wsStatus] = await Promise.all([
      api('/minipc'),
      fetch(location.origin+'/ws/status',{headers:TOKEN?{'Authorization':'Bearer '+TOKEN}:{}})
        .then(r=>r.ok?r.json():{connected:[]})
        .catch(()=>({connected:[]}))
    ]);

    if(!list.length){
      view.innerHTML=tabsHtml+'<div class="empty">Экранов пока нет. Нажмите «Добавить экран».</div>';
      return;
    }

    const wsConnected = new Set((wsStatus.connected||[]).map(Number));
    let h=tabsHtml+'<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(230px,1fr));">';

    list.forEach(s=>{
      const on=s.status==='online';
      const wsOn = wsConnected.has(Number(s.id));
      const wsBadge = wsOn
        ? `<span title="WebSocket активен — push-команды работают" style="font-size:10px;background:rgba(40,167,69,0.15);color:#28a745;border-radius:4px;padding:1px 6px;font-weight:600;">WS</span>`
        : `<span title="WebSocket не подключён (polling)" style="font-size:10px;background:rgba(108,117,125,0.1);color:var(--muted);border-radius:4px;padding:1px 6px;">WS</span>`;

      const groupLabel = s.group_name
        ? `<span style="font-size:10px;background:var(--panel);border-radius:4px;padding:1px 5px;color:var(--muted);">${esc(s.group_name)}</span>`
        : '';

      // Видеовыход: агент читает /sys/class/drm/*/status. null = агент старой
      // версии или ядро не отдаёт статус — тогда бейдж серый, тревоги нет.
      const outsTitle = s.display_outputs ? ' Выходы: ' + esc(s.display_outputs) + '.' : '';
      const dispBadge = s.display_connected === true
        ? `<span title="Монитор подключён к видеовыходу.${outsTitle}" style="font-size:10px;background:rgba(40,167,69,0.15);color:#28a745;border-radius:4px;padding:1px 6px;font-weight:600;">📺</span>`
        : s.display_connected === false
        ? `<span title="Монитор НЕ подключён к видеовыходу.${outsTitle}" style="font-size:10px;background:rgba(220,53,69,0.15);color:var(--danger);border-radius:4px;padding:1px 6px;font-weight:600;">📺</span>`
        : `<span title="Состояние видеовыхода неизвестно: агент старой версии или драйвер не сообщает статус" style="font-size:10px;background:rgba(108,117,125,0.1);color:var(--muted);border-radius:4px;padding:1px 6px;">📺</span>`;

      const VENUES = {store_alcohol:'Магазин (алко)', store:'Магазин', mall:'ТЦ', office:'Офис', other:''};
      const venueLabel = VENUES[s.venue_type] ? `🏬 ${VENUES[s.venue_type]} · ` : '';
      const powerLabel = (s.power_on_time && s.power_off_time)
        ? `⏻ ${String(s.power_on_time).slice(0,5)}–${String(s.power_off_time).slice(0,5)}`
        : '⏻ всегда';
      const volLabel = (s.night_from && s.night_to)
        ? `🔊 ${s.volume_day!=null?s.volume_day:100} / 🌙 ${s.volume_night!=null?s.volume_night:100}`
        : `🔊 ${s.volume_day!=null?s.volume_day:100}`;

      h+=`<div class="cell">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;flex-wrap:wrap;">
          <span class="dot" style="background:${on?'var(--accent)':'var(--danger)'};flex-shrink:0;"></span>
          <span style="color:var(--muted);font-size:11px;flex-shrink:0;" title="ID экрана — для установки агента">ID ${s.id}</span>
          <span style="font-weight:500;flex:1;">${esc(s.name)}</span>${wsBadge}${dispBadge}${groupLabel}
        </div>
        <div style="font-size:11px;color:var(--muted);margin-bottom:3px;">${esc(s.city||'')}${s.city&&s.location?' · ':''}${esc(s.location||'')} ${on?'· своб. '+(s.disk_free_gb!=null?Number(s.disk_free_gb).toFixed(0):'?')+' ГБ':'· офлайн'}</div>
        ${(s.os_version||s.vlc_version)?`<div style="font-size:11px;color:var(--muted);margin-bottom:3px;">ОС: ${esc(s.os_version||'—')} · VLC: ${esc(s.vlc_version||'—')}</div>`:''}
        <div style="font-size:11px;color:var(--muted);margin-bottom:3px;">${venueLabel}${powerLabel} · ${volLabel}</div>
        ${(s.clock_drift_seconds!=null && Math.abs(s.clock_drift_seconds)>60)
          ? `<div style="font-size:11px;color:var(--danger);margin-bottom:3px;" title="Разница между часами сервера и мини-ПК. Проверьте NTP/батарейку CMOS на устройстве — уплывшие часы ломают расписание, окна питания и синхронный показ.">⚠ Часы мини-ПК ${s.clock_drift_seconds>0?'отстают':'спешат'} на ${Math.round(Math.abs(s.clock_drift_seconds))} с</div>`
          : ''}
        ${(on && s.display_connected === false)
          ? `<div style="font-size:11px;color:var(--danger);margin-bottom:3px;" title="Мини-ПК работает и играет контент, но ядро не видит монитора на видеовыходе. Проверьте кабель HDMI/DisplayPort и питание монитора.${outsTitle}">⚠ Монитор отключён от видеовыхода${s.display_changed_at?' (с '+esc(fmtServerTS(s.display_changed_at))+')':''}</div>`
          : ''}
        <div style="font-size:11px;color:${on?'var(--txt2)':'var(--dim)'};margin-bottom:9px;">▶ ${esc(s.playing_file||'—')}</div>
        <div style="display:flex;gap:5px;flex-wrap:wrap;">${on
          ? `<button class="btn" title="Возобновить показ по расписанию" data-action="play-screen" data-screen-id="${s.id}" data-screen-name="${esc(s.name)}">▶ Показ</button>
             <button class="btn danger" data-action="stop-screen" data-screen-id="${s.id}" data-screen-name="${esc(s.name)}">■ Стоп</button>
             <button class="btn" title="Перезапустить агент ds-agent" data-action="restart-agent" data-screen-id="${s.id}" data-screen-name="${esc(s.name)}">↺ Агент</button>`
          : `<button class="btn" data-action="screen-offline-diagnostics">Диагностика</button>`}
          <button class="btn" title="Питание монитора и громкость по расписанию" data-action="open-screen-settings"
            data-screen-id="${s.id}" data-screen-name="${esc(s.name)}"
            data-pon="${s.power_on_time?String(s.power_on_time).slice(0,5):''}" data-poff="${s.power_off_time?String(s.power_off_time).slice(0,5):''}"
            data-vday="${s.volume_day!=null?s.volume_day:100}" data-vnight="${s.volume_night!=null?s.volume_night:100}"
            data-nfrom="${s.night_from?String(s.night_from).slice(0,5):''}" data-nto="${s.night_to?String(s.night_to).slice(0,5):''}"
            data-venue="${s.venue_type||'other'}">⏻ Настройки</button>
          <button class="btn" title="Агент соберёт архив логов и пришлёт на сервер (~15–30 с)" data-action="screen-collect-diag" data-screen-id="${s.id}">🧾 Диагностика</button>
          <button class="btn danger" data-action="delete-screen" data-screen-id="${s.id}" data-screen-name="${esc(s.name)}">Удалить</button>
        </div>
        <div id="scr-settings-${s.id}"></div>
        <div id="scr-diag-${s.id}"></div>
      </div>`;
    });

    view.innerHTML=h+'</div>';
  }catch(e){
    view.innerHTML=tabsHtml+'<div class="empty">Ошибка: '+esc(e.message)+'</div>';
  }
}

//=============================================================================
// ГРУППЫ ЭКРАНОВ (v15)
//=============================================================================
async function viewGroups(view, tabsHtml){
  try{
    const [groups, screens] = await Promise.all([api('/groups'), api('/minipc')]);
    let h = tabsHtml||'';

    if(!groups.length){
      h+='<div class="empty">Групп пока нет. Создайте первую группу для синхронного показа на нескольких экранах.</div>';
      view.innerHTML=h;
      return;
    }

    h+='<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(290px,1fr));">';

    groups.forEach(g=>{
      const gs = screens.filter(s=>s.group_id===g.id);
      const onl = gs.filter(s=>s.status==='online').length;

      const scrRows = gs.length
        ? gs.map(s=>{
            const on=s.status==='online';
            return `<div style="display:flex;align-items:center;gap:6px;padding:4px 0;border-bottom:.5px solid var(--border);">
              <span class="dot" style="background:${on?'var(--accent)':'var(--danger)'}"></span>
              <span style="flex:1;font-size:12px;">${esc(s.name)}</span>
              ${canWrite()
                ? `<button
                    data-action="remove-from-group"
                    data-group-id="${g.id}"
                    data-screen-id="${s.id}"
                    data-screen-name="${esc(s.name)}"
                    title="Убрать из группы"
                    style="background:none;border:none;color:var(--danger);cursor:pointer;padding:0 4px;font-size:13px;"
                  >✕</button>`
                : ''}
            </div>`;
          }).join('')
        : '<div class="muted" style="font-size:12px;">В группе пока нет экранов</div>';

      const availableScreens = JSON.stringify(
        screens
          .filter(s=>!s.group_id || s.group_id!==g.id)
          .map(s=>({id:s.id,name:s.name}))
      );

      h+=`<div class="cell">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:8px;">
          <div>
            <div style="font-weight:600;">${esc(g.name)}</div>
            <div class="muted" style="font-size:11px;">${esc(g.description||'')}</div>
          </div>
          <div class="muted" style="font-size:11px;">онлайн ${onl}/${gs.length}</div>
        </div>

        <div style="margin-bottom:10px;">${scrRows}</div>

        <div style="display:flex;gap:6px;flex-wrap:wrap;">
          ${gs.length
            ? `<button class="btn primary" data-action="open-sync-start-form" data-group-id="${g.id}" data-group-name="${esc(g.name)}">▶ Синхростарт</button>`
            : ''}
          <button
            class="btn"
            data-action="open-add-to-group-form"
            data-group-id="${g.id}"
            data-group-name="${esc(g.name)}"
            data-available-screens='${esc(availableScreens)}'
          >+ Экран</button>
          <button class="btn danger" data-action="delete-group" data-group-id="${g.id}" data-group-name="${esc(g.name)}">Удалить</button>
        </div>
      </div>`;
    });

    h+='</div>';

    const ungrouped = screens.filter(s=>!s.group_id);
    if(ungrouped.length){
      h+='<div class="sec">Экраны без группы</div><div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr));">';

      ungrouped.forEach(s=>{
        const on=s.status==='online';
        h+=`<div class="cell">
          <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;">
            <span class="dot" style="background:${on?'var(--accent)':'var(--danger)'}"></span>
            <span style="font-size:13px;font-weight:500;">${esc(s.name)}</span>
          </div>
          ${canWrite()
            ? `<select class="inp" style="font-size:11px;" data-action="assign-group" data-screen-id="${s.id}">
                <option value="">— Назначить группу —</option>
                ${groups.map(g=>`<option value="${g.id}">${esc(g.name)}</option>`).join('')}
              </select>`
            : '<div class="muted" style="font-size:11px;">без группы</div>'}
        </div>`;
      });

      h+='</div>';
    }

    view.innerHTML=h;
  }catch(e){
    view.innerHTML=(tabsHtml||'')+'<div class="empty">Ошибка: '+esc(e.message)+'</div>';
  }
}

async function assignGroup(screenId, groupId){
  if(!groupId) return;
  try{
    await api('/groups/'+groupId+'/screens/'+screenId, {method:'POST'});
    toast('Экран добавлен в группу');
    viewScreens('groups');
  }catch(e){
    toast('Ошибка: '+e.message);
  }
}

async function removeFromGroup(groupId, screenId, name){
  try{
    await api('/groups/'+groupId+'/screens/'+screenId, {method:'DELETE'});
    toast('«'+name+'» убран из группы');
    viewScreens('groups');
  }catch(e){
    toast('Ошибка: '+e.message);
  }
}

function addToGroupForm(groupId, groupName, available){
  if(!available.length){
    toast('Нет экранов без группы');
    return;
  }

  document.body.insertAdjacentHTML('beforeend',`<div style="position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:999;" id="modal-grp">
    <div style="background:var(--bg);border-radius:10px;padding:20px;min-width:300px;">
      <div style="font-weight:600;margin-bottom:12px;">Добавить экран в «${esc(groupName)}»</div>
      <select class="inp" id="modal-scr-sel">${available.map(s=>`<option value="${s.id}">${esc(s.name)}</option>`).join('')}</select>
      <div style="display:flex;gap:8px;margin-top:12px;">
        <button class="btn primary" data-action="confirm-add-to-group" data-group-id="${groupId}">Добавить</button>
        <button class="btn" data-action="close-group-modal">Отмена</button>
      </div>
    </div>
  </div>`);
}

async function confirmAddToGroup(groupId){
  const sel=document.getElementById('modal-scr-sel');
  if(!sel || !sel.value) return;

  try{
    await api('/groups/'+groupId+'/screens/'+sel.value,{method:'POST'});
    document.getElementById('modal-grp')?.remove();
    toast('Экран добавлен');
    viewScreens('groups');
  }catch(e){
    toast('Ошибка: '+e.message);
  }
}

async function playScreen(id, name){
  // «Показ» снимает флаг «Стоп» командой resume — агент сам возобновит показ по
  // расписанию на следующем цикле (без перезапуска/sudo, надёжно через опрос).
  try{
    await api('/command/resume/'+id, {method:'POST'});
    toast('Показ возобновляется…');
  }catch(e){
    toast('Ошибка: '+e.message);
  }
}

// Диагностика экрана: команда агенту + список присланных архивов
async function collectScreenDiag(id){
  try{
    await api('/minipc/' + id + '/collect-diag', {method:'POST'});
    toast('Команда отправлена — агент собирает архив (~15–30 с)');
    showScreenDiagList(id);
    setTimeout(() => showScreenDiagList(id), 20000);
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function showScreenDiagList(id){
  const el = document.getElementById('scr-diag-' + id);
  if(!el) return;
  try{
    const rows = await api('/minipc/' + id + '/diagnostics');
    let h = `<div style="border-top:0.5px solid var(--border2);margin-top:9px;padding-top:8px;">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
        <span class="muted" style="font-size:12px;">Архивы диагностики (последние ${rows.length})</span>
        <button class="btn" style="padding:3px 10px;font-size:11px;margin-left:auto;" data-action="screen-refresh-diag" data-screen-id="${id}">↻ Обновить</button>
      </div>`;
    if(!rows.length){
      h += '<div class="muted" style="font-size:12px;">Пока нет — нажмите «🧾 Диагностика» и обновите через полминуты.</div>';
    } else {
      rows.forEach(r => {
        h += `<div style="display:flex;align-items:center;gap:10px;font-size:12px;margin-bottom:4px;">
          <span>🧾 ${fmtServerTS(r.created_at)}</span>
          <span class="muted">${(r.size_bytes/1024).toFixed(0)} КБ</span>
          <a class="btn" style="padding:2px 10px;font-size:11px;margin-left:auto;" href="${API}/diagnostics/${r.id}/download">⤓ Скачать</a>
        </div>`;
      });
    }
    el.innerHTML = h + '</div>';
  }catch(e){
    el.innerHTML = `<div class="muted" style="font-size:12px;">Диагностика: ${esc(e.message)}</div>`;
  }
}

async function restartAgent(id, name){
  if(!confirm('Перезапустить агент на экране «'+(name||('#'+id))+'»? Показ прервётся на пару секунд.')) return;
  try{
    await api('/command/restart/'+id, {method:'POST'});
    toast('Команда перезапуска отправлена');
  }catch(e){
    toast('Ошибка: '+e.message);
  }
}

async function stopScreen(id, name){
  if(!confirm('Остановить показ на экране «'+(name||('#'+id))+'»? '
            + 'Это разовая остановка — агент возобновит показ по расписанию.')) return;
  try{
    await api('/command/stop/'+id, {method:'POST'});
    toast('Команда остановки отправлена');
  }catch(e){
    toast('Ошибка: '+e.message);
  }
}

async function delScreen(id, name){
  if(!confirm('Удалить экран «'+(name||('#'+id))+'»? Действие необратимо. '
            + 'Агент на мини-ПК перестанет получать расписание.')) return;

  try{
    await api('/minipc/'+id, {method:'DELETE'});
    toast('Экран удалён');
    viewScreens('screens');
  }catch(e){
    toast('Ошибка: '+e.message);
  }
}

async function deleteGroup(id, name){
  if(!confirm('Удалить группу «'+name+'»? Экраны останутся, но потеряют привязку.')) return;

  try{
    await api('/groups/'+id,{method:'DELETE'});
    toast('Группа удалена');
    viewScreens('groups');
  }catch(e){
    toast('Ошибка: '+e.message);
  }
}

function groupAddForm(){
  document.getElementById('topright').innerHTML='';
  document.getElementById('view').innerHTML=`<div style="max-width:430px;">
    <div class="fld"><label>Название группы</label><input class="inp" id="grp-name" placeholder="Зал 1 — Вход"></div>
    <div class="fld"><label>Описание (необязательно)</label><input class="inp" id="grp-desc" placeholder="Экраны у главного входа"></div>
    <div style="display:flex;gap:8px;">
      <button class="btn primary" data-action="submit-create-group">Создать</button>
      <button class="btn" data-action="cancel-create-group">Отмена</button>
    </div>
  </div>`;
}

async function createGroup(){
  const name=(document.getElementById('grp-name')||{}).value||'';
  const desc=(document.getElementById('grp-desc')||{}).value||'';

  if(!name){
    toast('Введите название');
    return;
  }

  try{
    await api('/groups',{method:'POST',body:JSON.stringify({name,description:desc})});
    toast('Группа создана');
    viewScreens('groups');
  }catch(e){
    toast('Ошибка: '+e.message);
  }
}

// Синхронный старт
function syncStartForm(groupId, groupName){
  document.body.insertAdjacentHTML('beforeend',`<div style="position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:999;" id="modal-sync">
    <div style="background:var(--bg);border-radius:10px;padding:20px;min-width:340px;max-width:460px;">
      <div style="font-weight:600;margin-bottom:4px;">▶ Синхронный старт</div>
      <div style="font-size:12px;color:var(--muted);margin-bottom:12px;">Группа: <b>${esc(groupName)}</b> · все экраны стартуют одновременно</div>
      <div class="fld">
        <label>Файл для воспроизведения</label>
        <select class="inp" id="sync-file-sel"><option>Загрузка…</option></select>
      </div>
      <div class="fld">
        <label>Время запуска (необязательно)</label>
        <input class="inp" id="sync-start-at" type="datetime-local">
      </div>
      <div style="display:flex;gap:8px;">
        <button class="btn primary" data-action="submit-sync-start" data-group-id="${groupId}">▶ Запустить</button>
        <button class="btn" data-action="close-sync-modal">Отмена</button>
      </div>
    </div>
  </div>`);

  loadSyncMedia();
}

async function loadSyncMedia(){
  const sel = document.getElementById('sync-file-sel');
  if(!sel) return;

  try{
    const items = await api('/media');
    if(!items.length){
      sel.innerHTML = '<option value="">Нет доступных файлов</option>';
      return;
    }

    sel.innerHTML = items.map(m=>`<option value="${esc(m.filename)}">${esc(m.title||m.filename)}</option>`).join('');
  }catch(e){
    sel.innerHTML = '<option value="">Ошибка загрузки</option>';
  }
}

async function doSyncStart(groupId){
  const file = (document.getElementById('sync-file-sel')||{}).value||'';
  const startAtInput = (document.getElementById('sync-start-at')||{}).value||'';

  if(!file){
    toast('Выберите файл');
    return;
  }

  try{
    await api('/groups/'+groupId+'/sync-start',{
      method:'POST',
      body: JSON.stringify({
        filename: file,
        start_at: startAtInput || null
      })
    });
    document.getElementById('modal-sync')?.remove();
    toast('Синхростарт отправлен');
  }catch(e){
    toast('Ошибка: '+e.message);
  }
}

function screenAddForm(){
  document.getElementById('topright').innerHTML='';
  document.getElementById('view').innerHTML=`<div style="max-width:460px;">
      <div class="fld"><label>Название экрана</label><input class="inp" id="scr-name" placeholder="Напр. miniPC на входе"></div>
      <div class="fld"><label>Город</label><input class="inp" id="scr-city" placeholder="Напр. Астана"></div>
      <div class="fld"><label>Локация</label><input class="inp" id="scr-loc" placeholder="Напр. ТЦ Мечта, 1 этаж"></div>
      <div style="display:flex;gap:8px;">
        <button class="btn primary" data-action="submit-create-screen">Создать и получить токен</button>
        <button class="btn" data-action="cancel-create-screen">Отмена</button>
      </div>
      <div id="scr-token-wrap"></div>
    </div>`;
}

async function createScreen(){
  const name=(document.getElementById('scr-name')||{}).value||'';
  const city=(document.getElementById('scr-city')||{}).value||'';
  const location=(document.getElementById('scr-loc')||{}).value||'';

  if(!name){
    toast('Введите название экрана');
    return;
  }

  try{
    // Бэкенд: POST /minipc/register (query-параметры) создаёт экран и сразу
    // возвращает токен одним запросом.
    const q = new URLSearchParams({name});
    if(city) q.append('city', city);
    if(location) q.append('location', location);
    const created = await api('/minipc/register?' + q, {method:'POST'});
    const token = created.token;
    const sid = created.id;
    const wrap = document.getElementById('scr-token-wrap');

    if(wrap){
      const cmd = `sudo bash install.sh http://${window.location.hostname} ${token} ${sid} toor`;
      const cbox = 'user-select:all;word-break:break-all;background:rgba(255,255,255,.05);padding:6px 8px;border-radius:8px;';
      wrap.innerHTML = `<div style="margin-top:12px;padding:12px;border:1px solid var(--border);border-radius:10px;background:var(--panel);">
        <div style="font-weight:600;margin-bottom:8px;">Экран создан — данные для установки агента</div>
        <div style="margin-bottom:8px;">ID экрана: <code style="${cbox}font-size:14px;font-weight:600;">${sid}</code></div>
        <div style="font-size:12px;color:var(--muted);margin-bottom:4px;">Токен (сохраните — повторно не показывается):</div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px;">
          <code style="${cbox}">${esc(token)}</code>
          <button class="btn" data-action="copy-screen-token" data-token="${esc(token)}">Копировать</button>
        </div>
        <div style="font-size:12px;color:var(--muted);margin-bottom:4px;">Команда установки на мини-ПК (проверьте пользователя, по умолчанию toor):</div>
        <code style="${cbox}display:block;">${esc(cmd)}</code>
      </div>`;
    }

    toast('Экран создан');
  }catch(e){
    toast('Ошибка: '+e.message);
  }
}
