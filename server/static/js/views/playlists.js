//=============================================================================
// ПЛЕЙЛИСТЫ
//=============================================================================

let CUR_PL = null;

// Блок «Где и когда идёт» в карточке плейлиста (GET /placements?playlist_id=):
let PLB_SEQ = 0;                 // номер последней загрузки блока — устаревший ответ отбрасываем
const PLB_ROWS = new Map();      // id показа -> строка ответа (для текста подтверждений)
const PLB_BUSY = new Set();      // id показов, по которым сейчас идёт запрос («Остановить» / «Отменить»)
const PLB_DELETING = new Set();  // id плейлистов, у которых открыто подтверждение удаления

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

      case 'playlists-edit-items': {
        const id = Number(el.dataset.playlistId);
        const name = el.dataset.playlistName || '';
        return Signage.openPlaylistEditor({
          playlistId: id,
          name,
          onSaved: r => openPlaylist(id, r.name)
        });
      }

      case 'playlists-send': {
        const id = Number(el.dataset.playlistId);
        return plbOpenEditor({ playlistId: id, name: el.dataset.playlistName });
      }

      case 'playlists-plc-edit':
        return plbOpenEditor({ playlistId: CUR_PL && CUR_PL.id, name: CUR_PL && CUR_PL.name,
          placementId: Number(el.dataset.placementId) });

      case 'playlists-plc-repeat':
        return plbOpenEditor({ playlistId: CUR_PL && CUR_PL.id, name: CUR_PL && CUR_PL.name,
          repeatFrom: Number(el.dataset.placementId) });

      case 'playlists-plc-stop':
        return plbStop(Number(el.dataset.placementId));

      case 'playlists-plc-cancel':
        return plbCancel(Number(el.dataset.placementId));

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

  // Показ создан или изменён в окне «Куда и когда» — обновим блок, если карточка этого плейлиста открыта
  document.addEventListener('placements-changed', e => {
    const pid = e.detail && Number(e.detail.playlistId);
    if(pid && CUR_PL && CUR_PL.id === pid) plbLoad(pid);
  });

  // Переключатель «фиксированная длительность» — прячем/показываем поле минут
  document.addEventListener('change', e => {
    if(e.target && e.target.id === 'pl-fill-on'){
      const wrap = document.getElementById('pl-fill-minutes-wrap');
      if(wrap) wrap.style.display = e.target.checked ? '' : 'none';
    }
  });
}

// Папки для выбора ролика (окно «Плейлист», js/views/playlist-editor.js):
// «Все», «Общая медиатека» (без рекламодателя и папки), общие папки,
// рекламодатель без папки, папки рекламодателей. media — ролики, из которых
// выбирают (без заглушек); показываются только папки, где есть такие ролики.
// -> [{key, label}], ключи: all | root | f:<id папки> | a:<id рекламодателя>
function buildMediaGroups(media, folders){
  folders = folders || [];
  const hasRoot = media.some(m => !m.advertiser_id && !m.folder_id);
  const folderIds = new Set(media.filter(m => m.folder_id).map(m => m.folder_id));
  const groups = [{key:'all', label:'Все ролики'}];
  if(hasRoot) groups.push({key:'root', label:'🗂 Общая медиатека (без папки)'});
  folders.filter(f => !f.advertiser_id && folderIds.has(f.id)).forEach(f =>
    groups.push({key:'f:'+f.id, label:'🗂 Общая / ' + f.name}));
  const advSeen = new Set();
  media.forEach(m => {
    if(m.advertiser_id && !m.folder_id && !advSeen.has(m.advertiser_id)){
      advSeen.add(m.advertiser_id);
      groups.push({key:'a:'+m.advertiser_id, label:(m.advertiser||('Рекламодатель '+m.advertiser_id)) + ' (без папки)'});
    }
  });
  folders.filter(f => f.advertiser_id && folderIds.has(f.id)).forEach(f =>
    groups.push({key:'f:'+f.id, label:(f.advertiser||'') + ' / ' + f.name}));
  return groups;
}

// Ролики группы с ключом из buildMediaGroups.
function filterMediaByGroup(list, key){
  if(key === 'root')            return list.filter(m => !m.advertiser_id && !m.folder_id);
  if(key.startsWith('f:'))      { const fid = Number(key.slice(2)); return list.filter(m => m.folder_id === fid); }
  if(key.startsWith('a:'))      { const aid = Number(key.slice(2)); return list.filter(m => m.advertiser_id === aid && !m.folder_id); }
  return list;
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

// «+ Плейлист»: окно с названием и плитками роликов (вместо prompt и пары списков)
function createPlaylistPrompt(){
  Signage.openPlaylistEditor({ onSaved: () => viewPlaylists() });
}

async function deletePlaylist(id, name){
  if(PLB_DELETING.has(id)) return;           // подтверждение уже открыто (двойной клик)
  PLB_DELETING.add(id);
  try{
    // Показы плейлиста уйдут вместе с ним — предупредим, сколько их идёт и ждёт
    let text = 'Удалить плейлист «' + name + '»?';
    try{
      const rows = await api('/placements?playlist_id=' + id);
      const live = (rows || []).filter(p => p.status === 'active' || p.status === 'planned').length;
      if(live) text = 'Плейлист стоит в ' + live + ' показах — они будут отменены. Удалить плейлист «' + name + '»?';
    }catch(e){
      if(!TOKEN) return;                     // сессия истекла — api() уже показал вход
      text = 'Не удалось проверить, стоит ли плейлист в показах — если стоит, они будут отменены. Удалить плейлист «' + name + '»?';
    }
    if(!confirm(text)) return;

    try{
      await api('/playlists/' + id, { method:'DELETE' });
      toast('Плейлист удалён');
      viewPlaylists();
    }catch(e){
      toast('Ошибка: ' + e.message);
    }
  }finally{
    PLB_DELETING.delete(id);
  }
}

async function openPlaylist(id, name){
  CUR_PL = { id, name };
  document.getElementById('topright').innerHTML = '';

  const view = document.getElementById('view');

  try{
    const items = await api('/playlists/' + id + '/items');

    let h = `<div style="display:flex;align-items:center;gap:9px;margin-bottom:13px;flex-wrap:wrap;">
      <button class="btn" data-action="playlists-back">← Назад</button>
      <span style="font-weight:500;flex:1 1 160px;min-width:0;overflow-wrap:anywhere;">▶ ${esc(name)}</span>
      ${canWrite() ? `<button class="btn"
        data-action="playlists-edit-items"
        data-playlist-id="${id}"
        data-playlist-name="${esc(name)}">✎ Изменить ролики</button>
      <button class="btn primary"
        data-action="playlists-send"
        data-playlist-id="${id}"
        data-playlist-name="${esc(name)}">📤 Отправить на экраны</button>` : ''}
    </div>
    <div id="pl-placements" class="plb" style="margin-bottom:13px;">
      <div class="plb-head"><b>Где и когда идёт</b></div>
      <div class="plb-empty">Загрузка…</div>
    </div>
    <div id="pl-fill-panel" style="margin-bottom:13px;"></div>`;

    if(!items.length){
      h += '<div class="empty">Плейлист пуст — нажмите «✎ Изменить ролики» и отметьте ролики.</div>';
      view.innerHTML = h;
      plbLoad(id);
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
    plbLoad(id);
    loadFillPanel(id);
  }catch(e){
    view.innerHTML = '<div class="empty">Ошибка: ' + esc(e.message) + '</div>';
  }
}

//-----------------------------------------------------------------------------
// Блок «Где и когда идёт» в карточке плейлиста: показы этого плейлиста
// (GET /placements?playlist_id=) — экраны, даты, часы, статус, строки «по
// очереди» / «уступает» и кнопки по статусу. Окно «Куда и когда» —
// js/views/placement-editor.js; после его сохранения приходит событие
// placements-changed (см. выше).
//-----------------------------------------------------------------------------

const PLB_STATUS = {
  active:  { text: 'идёт сейчас',  cls: 'now'  },
  planned: { text: 'запланирован', cls: 'plan' },
  done:    { text: 'закончился',   cls: 'done' },
  stopped: { text: 'остановлен',   cls: 'stop' },
};
const PLB_SCREENS_SHOWN = 4;     // сколько экранов показа перечислять сразу, остальные — под «и ещё N»
const PLB_ALT_SCREENS_SHOWN = 3; // и сколько экранов в строке «по очереди» / «уступает»

function plbPlural(n, one, few, many){
  const m10 = n % 10, m100 = n % 100;
  if(m10 === 1 && m100 !== 11) return one;
  if(m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

// api() бросает Error(тело ответа); у FastAPI 400/404/409 — {"detail": "текст"}
function plbErrText(e){
  const t = String((e && e.message) || e || '');
  try{
    const j = JSON.parse(t);
    if(j && typeof j.detail === 'string') return j.detail;
  }catch(_){}
  return 'Ошибка: ' + t;
}

// ГГГГ-ММ-ДД -> ДД.ММ.ГГГГ / ДД.ММ (строкой, без часовых поясов)
function plbDmy(iso){ const s = String(iso || ''); return s.slice(8, 10) + '.' + s.slice(5, 7) + '.' + s.slice(0, 4); }
function plbDm(iso){ const s = String(iso || ''); return s.slice(8, 10) + '.' + s.slice(5, 7); }

// «01.11 – 30.11.2026»; год у начала — только если он другой; один день — «01.11.2026»
function plbDates(a, b){
  if(a === b) return plbDmy(a);
  const same = String(a).slice(0, 4) === String(b).slice(0, 4);
  return (same ? plbDm(a) : plbDmy(a)) + ' – ' + plbDmy(b);
}

function plbHH(n){ return String(n).padStart(2, '0'); }
function plbHours(a, b){ return plbHH(a) + ':00 – ' + plbHH(b) + ':00'; }

// Строки решений по пересечениям: «по очереди» (mode alternate) и «уступает»
// (mode yield — в эти часы идёт другой плейлист). Одинаковые для нескольких
// экранов склеиваем в одну: «05.11–10.11, 10–12 ч: по очереди с «Акция» (на
// экранах: А, Б)», «05.11, 10–12 ч: уступает «Акция»». Экраны не называем, если
// строка относится ко всем экранам показа (или он на один экран).
function plbAlternates(p){
  const groups = new Map();
  (p.alternates || []).forEach(a => {
    const key = [a.mode, a.date_from, a.date_to, a.hour_from, a.hour_to, a.other_playlist_name].join('|');
    if(!groups.has(key)) groups.set(key, { a, screens: [] });
    const g = groups.get(key);
    if(!g.screens.includes(a.screen_name)) g.screens.push(a.screen_name);
  });
  const total = p.all_screens ? p.screen_count : (p.screens || []).length;
  return Array.from(groups.values()).map(({ a, screens }) => {
    const days = a.date_from === a.date_to ? plbDm(a.date_from)
      : (String(a.date_from).slice(0, 4) === String(a.date_to).slice(0, 4)
        ? plbDm(a.date_from) + '–' + plbDm(a.date_to)
        : plbDmy(a.date_from) + '–' + plbDmy(a.date_to));
    let where = '';
    if(total > 1 && screens.length < total){
      const shown = screens.slice(0, PLB_ALT_SCREENS_SHOWN).join(', ');
      const rest = screens.length - PLB_ALT_SCREENS_SHOWN;
      where = ' (на ' + (screens.length === 1 ? 'экране' : 'экранах') + ': ' + shown
        + (rest > 0 ? ' и ещё ' + rest : '') + ')';
    }
    const what = a.mode === 'yield' ? 'уступает «' : 'по очереди с «';
    return days + ', ' + plbHH(a.hour_from) + '–' + plbHH(a.hour_to) + ' ч: ' + what
      + a.other_playlist_name + '»' + where;
  });
}

function plbWhereHtml(p){
  if(p.all_screens) return `<b>Все экраны сети · ${esc(p.screen_count)}</b>`;
  const names = (p.screens || []).map(s => s.name);
  if(!names.length) return '<i>нет экранов</i>';
  if(names.length <= PLB_SCREENS_SHOWN) return names.map(n => `<b>${esc(n)}</b>`).join('');
  const head = names.slice(0, PLB_SCREENS_SHOWN - 1).map(n => `<b>${esc(n)}</b>`).join('');
  const rest = names.slice(PLB_SCREENS_SHOWN - 1);
  return head + `<details class="plb-more"><summary>и ещё ${esc(rest.length)}</summary>`
    + rest.map(n => `<b>${esc(n)}</b>`).join('') + '</details>';
}

// Показ одной строкой для текста подтверждения (это plain text — esc не нужен)
function plbDescribe(p){
  let where;
  if(p.all_screens) where = 'все экраны сети (' + p.screen_count + ')';
  else{
    const names = (p.screens || []).map(s => s.name);
    where = names.length > 3 ? names.slice(0, 3).join(', ') + ' и ещё ' + (names.length - 3) : names.join(', ');
  }
  return where + ', ' + plbDates(p.date_from, p.date_to) + ', ' + plbHours(p.hour_from, p.hour_to);
}

function plbActionsHtml(p){
  const id = esc(p.id);
  const btn = (action, label, cls) =>
    `<button type="button" class="btn${cls ? ' ' + cls : ''} plb-btn" data-action="${action}" data-placement-id="${id}"${PLB_BUSY.has(p.id) ? ' disabled' : ''}>${label}</button>`;
  switch(p.status){
    case 'active':  return btn('playlists-plc-edit', 'Изменить') + btn('playlists-plc-stop', 'Остановить', 'danger');
    case 'planned': return btn('playlists-plc-edit', 'Изменить') + btn('playlists-plc-cancel', 'Отменить', 'danger');
    case 'done':
    case 'stopped': return btn('playlists-plc-repeat', 'Повторить');
  }
  return '';
}

function plbRowHtml(p, write){
  const st = PLB_STATUS[p.status] || { text: p.status, cls: 'done' };
  // «по очереди» / «уступает» — только у идущих и запланированных: у закончившихся и остановленных они устарели
  const alts = (p.status === 'active' || p.status === 'planned')
    ? plbAlternates(p).map(t => `<span class="plb-alt">${esc(t)}</span>`).join('') : '';
  return `<tr>
    <td class="plb-scr">${plbWhereHtml(p)}</td>
    <td class="plb-nw">${esc(plbDates(p.date_from, p.date_to))}</td>
    <td class="plb-nw">${esc(plbHours(p.hour_from, p.hour_to))}</td>
    <td><span class="plb-st ${st.cls}">${esc(st.text)}</span>${alts}</td>
    ${write ? `<td><div class="plb-acts">${plbActionsHtml(p)}</div></td>` : ''}
  </tr>`;
}

function plbRender(box, rows){
  const write = canWrite();
  PLB_ROWS.clear();
  rows.forEach(p => PLB_ROWS.set(p.id, p));
  const n = rows.length;
  const count = n ? `<span class="plb-count">${n} ${plbPlural(n, 'показ', 'показа', 'показов')}</span>` : '';
  let body;
  if(!n){
    body = `<div class="plb-empty">${write
      ? 'Плейлист пока не отправлен на экраны — нажмите «📤 Отправить на экраны».'
      : 'Плейлист пока не отправлен на экраны.'}</div>`;
  }else{
    body = `<div class="plb-scroll"><table class="plb-table">
      <tr><th>Экраны</th><th>Даты</th><th>Часы</th><th>Статус</th>${write ? '<th></th>' : ''}</tr>
      ${rows.map(p => plbRowHtml(p, write)).join('')}
    </table></div>`;
  }
  box.innerHTML = `<div class="plb-head"><b>Где и когда идёт</b>${count}</div>${body}`;
}

// Загрузить показы плейлиста в блок карточки (первый раз и после любого изменения)
async function plbLoad(pid){
  const box = document.getElementById('pl-placements');
  if(!box) return;
  const seq = ++PLB_SEQ;
  try{
    const rows = await api('/placements?playlist_id=' + pid);
    // карточка успела закрыться / перерисоваться, или пришёл более свежий ответ
    if(seq !== PLB_SEQ || document.getElementById('pl-placements') !== box) return;
    plbRender(box, Array.isArray(rows) ? rows : []);
  }catch(e){
    if(seq !== PLB_SEQ || document.getElementById('pl-placements') !== box) return;
    PLB_ROWS.clear();
    box.innerHTML = `<div class="plb-head"><b>Где и когда идёт</b></div>
      <div class="plb-empty">Не удалось получить показы: ${esc(plbErrText(e))}</div>`;
  }
}

// Окно «Куда и когда»: новый показ, правка (placementId) или «Повторить» (repeatFrom)
function plbOpenEditor(opts){
  if(typeof Signage.openPlacementEditor !== 'function'){ toast('Окно отправки на экраны не загружено'); return; }
  if(!opts.playlistId) return;
  return Signage.openPlacementEditor({
    playlistId: opts.playlistId,
    name: opts.name || undefined,
    placementId: opts.placementId,
    repeatFrom: opts.repeatFrom,
  });
}

// Запрос по одному показу: пока он идёт, кнопки этой строки заблокированы; по
// итогу (и успех, и 409 «статус уже другой») блок перечитывается с сервера
async function plbRequest(placementId, method, path, okText){
  const pid = CUR_PL && CUR_PL.id;
  if(!pid || PLB_BUSY.has(placementId)) return;
  PLB_BUSY.add(placementId);
  document.querySelectorAll(`#pl-placements [data-placement-id="${placementId}"]`)
    .forEach(b => { b.disabled = true; });
  try{
    await api(path, { method });
    toast(okText);
  }catch(e){
    toast(plbErrText(e));
  }finally{
    PLB_BUSY.delete(placementId);
  }
  if(CUR_PL && CUR_PL.id === pid) plbLoad(pid);
}

function plbStop(placementId){
  const p = PLB_ROWS.get(placementId);
  if(!p || PLB_BUSY.has(placementId)) return;
  if(!confirm('Остановить показ?\n\n' + plbDescribe(p)
    + '\n\nС этого момента плейлист на экранах идти не будет. Уже прошедшие часы останутся в истории.')) return;
  return plbRequest(placementId, 'POST', '/placements/' + placementId + '/stop', 'Показ остановлен');
}

function plbCancel(placementId){
  const p = PLB_ROWS.get(placementId);
  if(!p || PLB_BUSY.has(placementId)) return;
  if(!confirm('Отменить запланированный показ?\n\n' + plbDescribe(p) + '\n\nПоказ будет удалён.')) return;
  return plbRequest(placementId, 'DELETE', '/placements/' + placementId, 'Показ отменён');
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
window.Signage.buildMediaGroups = buildMediaGroups;
window.Signage.filterMediaByGroup = filterMediaByGroup;
window.Signage.removeFromPlaylist = removeFromPlaylist;
window.Signage.savePlaylistFill = savePlaylistFill;

initPlaylistsViewActions();

