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
    }
  });
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
      h += `<div class="cell" style="display:flex;align-items:center;gap:10px;cursor:pointer;margin-bottom:7px;"
        data-action="playlists-open"
        data-playlist-id="${p.id}"
        data-playlist-name="${esc(p.name)}">
        <span style="min-width:30px;text-align:center;font-size:12px;font-weight:600;color:var(--muted);">№${i + 1}</span>
        <span style="font-size:18px;color:var(--accent);">▶</span>
        <span style="font-weight:500;flex:1;">${esc(p.name)}</span>
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
    const [items, media] = await Promise.all([
      api('/playlists/' + id + '/items'),
      api('/media')
    ]);

    let h = `<div style="display:flex;align-items:center;gap:9px;margin-bottom:13px;">
      <button class="btn" data-action="playlists-back">← Назад</button>
      <span style="font-weight:500;flex:1;">▶ ${esc(name)}</span>
    </div>`;

    if(canWrite()){
      const mediaOpts = media.map(m => `<option value="${m.id}">${esc(m.title || m.filename)}</option>`).join('');
      h += `<div style="display:flex;gap:8px;margin-bottom:14px;align-items:center;">
        <select class="inp" id="pl-add-media" style="flex:1;">${mediaOpts || '<option value="">нет файлов</option>'}</select>
        <input class="inp" id="pl-add-repeat" type="number" min="1" max="99" value="1" style="width:60px;" title="Повторов">
        <button class="btn primary" data-action="playlists-add-item" data-playlist-id="${id}">+ Добавить</button>
      </div>`;
    }

    if(!items.length){
      h += '<div class="empty">Плейлист пуст — добавьте ролики выше.</div>';
      view.innerHTML = h;
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
  }catch(e){
    view.innerHTML = '<div class="empty">Ошибка: ' + esc(e.message) + '</div>';
  }
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

initPlaylistsViewActions();

