//=============================================================================
// ПЛЕЙЛИСТЫ
//=============================================================================

let CUR_PL = null;

function initPlaylistsViewActions(){
  if(window.__playlistsViewActionsInitialized) return;
  window.__playlistsViewActionsInitialized = true;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('playlists-')) return;

    switch(action){
      case 'playlists-create':
        return Signage.createPlaylistPrompt();

      case 'playlists-open': {
        const id = Number(el.dataset.playlistId);
        const name = el.dataset.playlistName || '';
        return Signage.openPlaylist(id, name);
      }

      case 'playlists-delete': {
        e.stopPropagation();
        const id = Number(el.dataset.playlistId);
        const name = el.dataset.playlistName || '';
        return Signage.deletePlaylist(id, name);
      }

      case 'playlists-back':
        return Signage.nav('playlists');

      case 'playlists-add-item': {
        const id = Number(el.dataset.playlistId);
        return Signage.addToPlaylist(id);
      }

      case 'playlists-remove-item': {
        const playlistId = Number(el.dataset.playlistId);
        const itemId = Number(el.dataset.itemId);
        return Signage.removeFromPlaylist(playlistId, itemId);
      }

      case 'playlists-save-fill': {
        const id = Number(el.dataset.playlistId);
        return Signage.savePlaylistFill(id);
      }
    }
  });

  // Переключатель «фиксированная длительность» — прячем/показываем поле минут
  document.addEventListener('change', e => {
    if(e.target && e.target.id === 'pl-fill-on'){
      const wrap = document.getElementById('pl-fill-minutes-wrap');
      if(wrap) wrap.style.display = e.target.checked ? '' : 'none';
    }
    if(e.target && e.target.id === 'pl-add-folder'){
      plFillMediaSelect();
    }
  });
}

// Ролики выбранной папки → во второй селект
let PL_PICK_MEDIA = [];
function plFillMediaSelect(){
  const folderSel = document.getElementById('pl-add-folder');
  const mediaSel = document.getElementById('pl-add-media');
  if(!folderSel || !mediaSel) return;
  const key = folderSel.value;
  let list = PL_PICK_MEDIA;
  if(key === 'root')             list = list.filter(m => !m.advertiser_id && !m.folder_id);
  else if(key.startsWith('f:'))  { const fid = Number(key.slice(2)); list = list.filter(m => m.folder_id === fid); }
  else if(key.startsWith('a:'))  { const aid = Number(key.slice(2)); list = list.filter(m => m.advertiser_id === aid && !m.folder_id); }
  mediaSel.innerHTML = list.length
    ? list.map(m => `<option value="${m.id}">${esc(m.title || m.filename)}</option>`).join('')
    : '<option value="">— в этой папке нет роликов —</option>';
}

async function viewPlaylists(){
  CUR_PL = null;

  document.getElementById('topright').innerHTML = canWrite()
    ? `<button class="btn primary" data-action="playlists-create">+ Плейлист</button>`
    : '';

  const view = document.getElementById('view');

  try{
    const pls = await api('/playlists');

    if(!pls.length){
      view.innerHTML = '<div class="empty">Плейлистов пока нет. Нажмите «+ Плейлист» чтобы создать.</div>';
      return;
    }

    let h = '<div class="muted" style="font-size:12px;margin-bottom:11px;">Плейлист — набор роликов, воспроизводимых по порядку. Назначается экрану через Расписание. «№» — порядковый номер (тот же номер используется при выборе плейлиста в Расписании).</div>';

    pls.forEach((p, i) => {
      const fillOn = p.fill_to_hour !== false;
      const modeBadge = fillOn
        ? `<span class="muted" style="font-size:11px;background:var(--bg2);border-radius:5px;padding:2px 8px;">блок ${Math.round((p.target_seconds||3600)/60)} мин</span>`
        : `<span class="muted" style="font-size:11px;background:var(--bg2);border-radius:5px;padding:2px 8px;">произвольный</span>`;
      h += `<div class="cell" style="display:flex;align-items:center;gap:10px;cursor:pointer;margin-bottom:7px;"
        data-action="playlists-open"
        data-playlist-id="${p.id}"
        data-playlist-name="${esc(p.name)}">
        <span style="min-width:30px;text-align:center;font-size:12px;font-weight:600;color:var(--muted);">№${i + 1}</span>
        <span style="font-size:18px;color:var(--accent);">▶</span>
        <span style="font-weight:500;flex:1;">${esc(p.name)}</span>
        ${modeBadge}
        ${canWrite() ? `<button class="btn danger" style="padding:4px 10px;font-size:12px;"
          data-action="playlists-delete"
          data-playlist-id="${p.id}"
          data-playlist-name="${esc(p.name)}">🗑</button>` : ''}
        <span class="dim">›</span>
      </div>`;
    });

    view.innerHTML = h;
  }catch(e){
    view.innerHTML = '<div class="empty">Ошибка: ' + esc(e.message) + '</div>';
  }
}

async function createPlaylistPrompt(){
  const name = prompt('Название плейлиста:');
  if(!name || !name.trim()) return;

  try{
    await api('/playlists?name=' + encodeURIComponent(name.trim()), { method:'POST' });
    toast('Плейлист создан');
    viewPlaylists();
  }catch(e){
    toast('Ошибка: ' + e.message);
  }
}

async function deletePlaylist(id, name){
  if(!confirm('Удалить плейлист «' + name + '»?')) return;

  try{
    await api('/playlists/' + id, { method:'DELETE' });
    toast('Плейлист удалён');
    viewPlaylists();
  }catch(e){
    toast('Ошибка: ' + e.message);
  }
}

async function openPlaylist(id, name){
  CUR_PL = { id, name };
  document.getElementById('topright').innerHTML = '';

  const view = document.getElementById('view');

  try{
    const [items, media, folders] = await Promise.all([
      api('/playlists/' + id + '/items'),
      api('/media'),
      api('/media/folders-all').catch(() => [])
    ]);

    let h = `<div style="display:flex;align-items:center;gap:9px;margin-bottom:13px;">
      <button class="btn" data-action="playlists-back">← Назад</button>
      <span style="font-weight:500;flex:1;">▶ ${esc(name)}</span>
    </div>
    <div id="pl-fill-panel" style="margin-bottom:13px;"></div>`;

    if(canWrite()){
      // Заглушки в выпадашке не показываем: они добавляются в блок автоматически
      PL_PICK_MEDIA = media.filter(m => !m.is_filler);
      // Папки для первого селекта: «Все», «Общая медиатека» (без рекламодателя
      // и папки), общие папки, рекламодатель без папки, папки рекламодателей
      // Показываем только папки, где есть подходящие ролики (заглушки не в счёт)
      const hasRoot = PL_PICK_MEDIA.some(m => !m.advertiser_id && !m.folder_id);
      const folderIds = new Set(PL_PICK_MEDIA.filter(m => m.folder_id).map(m => m.folder_id));
      const groups = [{key:'all', label:'Все ролики'}];
      if(hasRoot) groups.push({key:'root', label:'🗂 Общая медиатека (без папки)'});
      folders.filter(f => !f.advertiser_id && folderIds.has(f.id)).forEach(f =>
        groups.push({key:'f:'+f.id, label:'🗂 Общая / ' + f.name}));
      const advSeen = new Set();
      PL_PICK_MEDIA.forEach(m => {
        if(m.advertiser_id && !m.folder_id && !advSeen.has(m.advertiser_id)){
          advSeen.add(m.advertiser_id);
          groups.push({key:'a:'+m.advertiser_id, label:(m.advertiser||('Рекламодатель '+m.advertiser_id)) + ' (без папки)'});
        }
      });
      folders.filter(f => f.advertiser_id && folderIds.has(f.id)).forEach(f =>
        groups.push({key:'f:'+f.id, label:(f.advertiser||'') + ' / ' + f.name}));

      h += `<div style="display:flex;gap:8px;margin-bottom:14px;align-items:center;flex-wrap:wrap;">
        <select class="inp" id="pl-add-folder" style="min-width:220px;" title="Из какой папки выбирать ролик">
          ${groups.map(g => `<option value="${g.key}">${esc(g.label)}</option>`).join('')}</select>
        <select class="inp" id="pl-add-media" style="flex:1;min-width:220px;"></select>
        <input class="inp" id="pl-add-repeat" type="number" min="1" max="99" value="1" style="width:60px;" title="Повторов">
        <button class="btn primary" data-action="playlists-add-item" data-playlist-id="${id}">+ Добавить</button>
      </div>`;
    }

    if(!items.length){
      h += '<div class="empty">Плейлист пуст — добавьте ролики выше.</div>';
      view.innerHTML = h;
      plFillMediaSelect();
      loadFillPanel(id);
      return;
    }

    h += '<table style="width:100%;border-collapse:collapse;">';
    h += '<tr style="font-size:11px;color:var(--muted);"><th style="text-align:left;padding:4px 8px;">#</th><th style="text-align:left;padding:4px 8px;">Ролик</th><th style="padding:4px 8px;">Повторов</th><th style="padding:4px 8px;">Длит.</th><th></th></tr>';

    items.forEach(it => {
      h += `<tr style="border-top:0.5px solid var(--border2);">
        <td style="padding:8px;color:var(--muted);font-size:12px;">${it.position}</td>
        <td style="padding:8px;font-size:13px;">${esc(it.title || it.filename)}</td>
        <td style="padding:8px;text-align:center;font-size:12px;">${it.repeat_count}×</td>
        <td style="padding:8px;text-align:center;font-size:12px;color:var(--muted);">${it.duration_seconds ? Math.round(it.duration_seconds) + 'с' : '—'}</td>
        <td style="padding:8px;text-align:right;">
          ${canWrite() ? `<button class="iconbtn del"
            data-action="playlists-remove-item"
            data-playlist-id="${id}"
            data-item-id="${it.id}">🗑</button>` : ''}
        </td>
      </tr>`;
    });

    h += '</table>';
    view.innerHTML = h;
    plFillMediaSelect();
    loadFillPanel(id);
  }catch(e){
    view.innerHTML = '<div class="empty">Ошибка: ' + esc(e.message) + '</div>';
  }
}

// Панель «Длительность эфирного блока»: режим, факт/цель, предупреждения
async function loadFillPanel(pid){
  const box = document.getElementById('pl-fill-panel');
  if(!box) return;
  try{
    const d = await api('/playlists/' + pid + '/fill_info');
    const mins = s => (s/60).toFixed(1).replace(/\.0$/,'');
    let status;
    if(!d.fill_to_hour){
      status = `<span class="muted" style="font-size:12px;">Произвольный плейлист: ролики играют по кругу как есть — ${mins(d.main_seconds)} мин, заглушки не добавляются.</span>`;
    } else {
      const okFill = !d.warnings.length;
      status = `<span style="font-size:12px;color:${okFill?'var(--accent)':'var(--c-nike)'};">
        Основные ролики: ${d.main_plays} · ${mins(d.main_seconds)} мин.
        Заглушками добавится: ${d.filler_plays} показ(ов) · ${mins(d.filler_seconds)} мин.
        Итого блок: <b>${mins(d.total_seconds)} из ${mins(d.target_seconds)} мин</b>.
        В медиатеке заглушек: ${d.fillers_available} шт · ${mins(d.fillers_available_seconds)} мин.</span>`;
    }
    const warn = d.warnings.map(w =>
      `<div style="font-size:12px;color:var(--danger);margin-top:6px;">⚠ ${esc(w)}</div>`).join('');
    const ctl = canWrite() ? `
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:8px;">
        <label style="font-size:12px;display:inline-flex;align-items:center;gap:6px;cursor:pointer;">
          <input type="checkbox" id="pl-fill-on" ${d.fill_to_hour?'checked':''} style="width:15px;height:15px;cursor:pointer;">
          Эфирный блок фиксированной длины (добивать заглушками)</label>
        <span id="pl-fill-minutes-wrap" style="display:${d.fill_to_hour?'':'none'};align-items:center;gap:5px;">
          <input class="inp" id="pl-fill-minutes" type="number" min="1" max="1440" value="${Math.round(d.target_seconds/60)}" style="width:70px;padding:5px 8px;text-align:right;">
          <span class="muted" style="font-size:12px;">мин</span>
        </span>
        <button class="btn" style="padding:5px 12px;font-size:12px;" data-action="playlists-save-fill" data-playlist-id="${pid}">💾 Сохранить режим</button>
      </div>` : '';
    box.innerHTML = `<div class="cell">${status}${warn}${ctl}</div>`;
  }catch(e){
    box.innerHTML = `<div class="muted" style="font-size:12px;">Не удалось получить расчёт блока: ${esc(e.message)}</div>`;
  }
}

async function savePlaylistFill(pid){
  const on = !!document.getElementById('pl-fill-on')?.checked;
  const minutes = parseInt(document.getElementById('pl-fill-minutes')?.value, 10) || 60;
  try{
    await api('/playlists/' + pid + '/fill', {method:'PATCH',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({fill_to_hour: on, target_minutes: minutes})});
    toast(on ? `Эфирный блок: ${minutes} мин` : 'Плейлист произвольный (без заглушек)');
    loadFillPanel(pid);
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function addToPlaylist(pid){
  const mid = document.getElementById('pl-add-media')?.value;
  const rc = parseInt(document.getElementById('pl-add-repeat')?.value) || 1;

  if(!mid){
    toast('Выберите ролик');
    return;
  }

  try{
    const res = await api(`/playlists/${pid}/items?media_id=${mid}&repeat_count=${rc}`, { method:'POST' });
    toast(res && res.warning ? ('Добавлен. ⚠ ' + res.warning) : 'Ролик добавлен');
    openPlaylist(pid, CUR_PL?.name || '');
  }catch(e){
    toast('Ошибка: ' + e.message);
  }
}

async function removeFromPlaylist(pid, itemId){
  if(!confirm('Удалить ролик из плейлиста?')) return;

  try{
    await api(`/playlists/${pid}/items/${itemId}`, { method:'DELETE' });
    toast('Удалено');
    openPlaylist(pid, CUR_PL?.name || '');
  }catch(e){
    toast('Ошибка: ' + e.message);
  }
}

window.Signage = window.Signage || {};
window.Signage.viewPlaylists = viewPlaylists;
window.Signage.createPlaylistPrompt = createPlaylistPrompt;
window.Signage.deletePlaylist = deletePlaylist;
window.Signage.openPlaylist = openPlaylist;
window.Signage.addToPlaylist = addToPlaylist;
window.Signage.removeFromPlaylist = removeFromPlaylist;
window.Signage.savePlaylistFill = savePlaylistFill;

initPlaylistsViewActions();

