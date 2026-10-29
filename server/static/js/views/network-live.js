//=============================================================================
// ЭФИР СЕТИ: что сейчас идёт на экранах
//
//   Signage.viewNetworkLive()    — открыть раздел (меню «Эфир сети», layout.js);
//   Signage.refreshNetworkLive() — перечитать данные сразу, ничего не сбрасывая
//                                  (для полосы общего эфира и окон правки);
//   Signage.stopNetworkLive()    — остановить автообновление (уход из раздела).
//
// Данные — GET /network/live одним запросом: шапка «N экранов · M в сети · K не в
// сети · обновлено ЧЧ:ММ:СС», поиск, таблица «Экран / Сейчас идёт / Играет ролик /
// Дальше» (при включённом общем эфире последняя колонка — «После эфира»). Плашка
// плейлиста окрашена цветом плейлиста (schPlColor, как в «Расписании»); клик по ней
// открывает справа панель «О плейлисте» (data-action nl-open-playlist): число
// роликов и показов, режим блока, на каких экранах идёт сейчас, ролики (клик —
// просмотр), «Где и когда идёт», слоты недельного шаблона, кнопки «✎ Изменить
// плейлист» (окно правки поверх страницы) и «📤 Отправить на экраны» (только с
// правом записи).
//
// Страница сама перечитывает данные каждые 30 с, пока открыта и вкладка видима.
// Поиск, открытая панель (перерисовывается для того же плейлиста), прокрутка и
// фокус при этом сохраняются: поле поиска и контейнер полосы эфира не
// перерисовываются, а таблица и панель — только если их разметка изменилась.
// Окна (openModal) не трогаются вовсе.
//
// Время от сервера — наивное МСК («2026-10-29T13:00:00»): ЧЧ:ММ берётся срезом
// строки, через new Date() его пропускать нельзя (сдвинет часовой пояс).
// Место под полосу общего эфира — <div id="nl-broadcast">; после каждой загрузки
// вызывается Signage.renderBroadcastBar?.(данные /network/live).
//=============================================================================
(function(){
  const REFRESH_MS = 30000;      // автообновление
  const DRAWER_ID = 'nl-drawer';

  let S = null;                  // состояние открытого раздела (один на всё приложение)

  // ---- мелочи ---------------------------------------------------------------

  const $ = id => document.getElementById(id);

  function plural(n, one, few, many){
    const m10 = n % 10, m100 = n % 100;
    if(m10 === 1 && m100 !== 11) return one;
    if(m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }

  // «на 1 экране» / «на 2 экранах» (предложный падеж)
  function onScreens(n){
    return 'на ' + n + ' ' + (n % 10 === 1 && n % 100 !== 11 ? 'экране' : 'экранах');
  }

  function fmtDur(sec){
    sec = Math.round(Number(sec) || 0);
    if(sec <= 0) return '0:00';
    const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    const pad = n => String(n).padStart(2, '0');
    return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  }

  // Поиск без учёта регистра и «ё» = «е»
  function norm(s){
    return String(s == null ? '' : s).toLowerCase().replace(/ё/g, 'е');
  }

  // api() бросает Error(тело ответа); у FastAPI это {"detail": "текст"}
  function errText(e){
    if(e instanceof TypeError) return 'нет связи с сервером';
    const t = String((e && e.message) || e || '').trim();
    try{
      const j = JSON.parse(t);
      if(j && typeof j.detail === 'string') return j.detail;
      if(j && Array.isArray(j.detail)) return 'некорректный запрос';
    }catch(_){}
    if(/^</.test(t)) return 'сервер не отвечает';     // страница-заглушка nginx (502/504)
    return t.slice(0, 200) || 'неизвестная ошибка';
  }

  // «2026-10-29T13:00:00» -> «13:00». Ночь следующих суток (конец эфира «до 24:00») -> «24:00».
  function fmtTime(iso, refIso){
    const s = String(iso || '');
    if(s.length < 16) return '';
    const hm = s.slice(11, 16);
    if(refIso && s.slice(0, 10) !== String(refIso).slice(0, 10)){
      return hm === '00:00' ? '24:00' : s.slice(8, 10) + '.' + s.slice(5, 7) + ' ' + hm;
    }
    return hm;
  }

  function fmtClock(iso){ return String(iso || '').slice(11, 19); }

  // ГГГГ-ММ-ДД -> ДД.ММ.ГГГГ / ДД.ММ
  function dmy(iso){ const s = String(iso || ''); return s.slice(8, 10) + '.' + s.slice(5, 7) + '.' + s.slice(0, 4); }
  function dm(iso){ const s = String(iso || ''); return s.slice(8, 10) + '.' + s.slice(5, 7); }
  function dates(a, b){
    if(a === b) return dmy(a);
    return (String(a).slice(0, 4) === String(b).slice(0, 4) ? dm(a) : dmy(a)) + ' – ' + dmy(b);
  }
  const hh = n => String(n).padStart(2, '0');

  function alive(st){ return S === st && !!$('nl-root'); }

  // ---- плашки и подписи источника ---------------------------------------------

  // Плейлист вида {playlist_ids, playlists}: i-я плашка (клик — панель «О плейлисте»)
  function plaque(v, i, st){
    const id = Number(v.playlist_ids[i]);
    const name = v.playlists[i] || ((st.data.playlists[String(id)] || {}).name) || ('Плейлист #' + id);
    const color = schPlColor(id);
    return `<button type="button" class="nl-pl${st.pid === id ? ' sel' : ''}"
      style="background:${hexA(color, .14)};border-color:${hexA(color, .5)};"
      data-action="nl-open-playlist" data-playlist-id="${id}" data-playlist-name="${esc(name)}"
      title="О плейлисте «${esc(name)}»">${esc(name)}</button>`;
  }

  const src = text => text ? `<span class="nl-src">${esc(text)}</span>` : '';

  const KIND_LABEL = {template: 'по расписанию', placement: '📅 показ', override: 'замена на дату'};

  // Источник и плейлисты одной клетки (now / next / after_broadcast); timeText — «до 13:00» / «с 13:00» / ''
  function viewHtml(v, timeText, st){
    const tail = timeText ? ' ' + timeText : '';
    if(v.kind === 'off') return src('выключено (замена на дату)' + tail);
    if(v.kind === 'empty' || !v.playlist_ids.length) return src('нет расписания — заглушки' + tail);
    if(v.kind === 'alternate' && v.playlist_ids.length > 1){
      const rest = v.playlist_ids.slice(1).map((_, i) => plaque(v, i + 1, st));
      return [plaque(v, 0, st), src('⇄ по очереди с'), ...rest, src(timeText)].filter(Boolean).join(' ');
    }
    const all = v.playlist_ids.map((_, i) => plaque(v, i, st));
    return [...all, src(((KIND_LABEL[v.kind] || '') + tail).trim())].filter(Boolean).join(' ');
  }

  function nowHtml(s, st){
    const d = st.data, v = s.now;
    let h;
    if(v.kind === 'broadcast'){
      const bar = v.playlist_ids.map((_, i) => plaque(v, i, st));
      h = [...bar, src(s.online
        ? '📡 общий эфир ' + (v.until ? 'до ' + fmtTime(v.until, d.generated_at) : 'до отмены')
        : '📡 получит при подключении')].join(' ');
    }else{
      h = viewHtml(v, v.until ? 'до ' + fmtTime(v.until, d.generated_at) : '', st);
    }
    if(s.broadcast_skipped){
      h += '<div class="nl-skip">📡 общий эфир на этом экране не идёт — нечего играть</div>';
    }
    return h;
  }

  function nextHtml(s, st){
    const d = st.data;
    if(d.broadcast.is_on){
      return s.after_broadcast ? viewHtml(s.after_broadcast, '', st) : '<span class="dim">—</span>';
    }
    if(!s.next) return '<span class="nl-src nl-flat">до конца дня без изменений</span>';
    return viewHtml(s.next, 'с ' + fmtTime(s.next.from, d.generated_at), st);
  }

  // ---- строка таблицы -----------------------------------------------------------

  function addressOf(s){
    return [s.city, s.location].filter(Boolean).join(', ');
  }

  function thumbHtml(mediaId){
    return `<span class="nl-th" aria-hidden="true">▶<img src="${API}/media/${Number(mediaId)}/thumbnail" alt="" loading="lazy" onerror="this.remove()"></span>`;
  }

  function clipHtml(s){
    const p = s.playing;
    const name = p ? (p.title || p.filename || '') : '';
    if(!s.online){
      return p ? `<span class="dim">нет связи — последний: ${esc(name)}</span>` : '<span class="dim">нет связи</span>';
    }
    if(!p) return '<span class="dim">—</span>';
    if(p.media_id == null){                       // ролик удалён из медиатеки: только имя файла
      return `<span class="nl-clip"><span class="nl-th" aria-hidden="true">▶</span><span class="nl-cn">${esc(name)}</span></span>`;
    }
    return `<button type="button" class="nl-clip nl-clipbtn" data-action="nl-preview-clip" data-media-id="${Number(p.media_id)}"
      title="Смотреть ролик">${thumbHtml(p.media_id)}<span class="nl-cn">${esc(name)}</span></button>`;
  }

  function rowHtml(s, st){
    const addr = addressOf(s);
    const sub = s.online
      ? (addr ? `<i>${esc(addr)}</i>` : '')
      : `<i title="${esc(addr)}">${esc(fmtOfflineFor(s.offline_for_seconds))}</i>`;
    return `<tr data-screen-id="${Number(s.id)}">
      <td><div class="nl-scr"><span class="nl-dot${s.online ? '' : ' off'}" role="img" aria-label="${s.online ? 'в сети' : 'не в сети'}"></span>
        <span class="nl-scr-n"><b>${esc(s.name)}</b>${sub}</span></div></td>
      <td>${nowHtml(s, st)}</td>
      <td>${clipHtml(s)}</td>
      <td>${nextHtml(s, st)}</td>
    </tr>`;
  }

  // Строка подходит под поиск: экран, город, место, названия плейлистов в строке
  function matches(s, qn){
    if(!qn) return true;
    const names = [s.name, s.city, s.location];
    [s.now, s.next, s.after_broadcast].forEach(v => { if(v) names.push(...v.playlists); });
    return norm(names.filter(Boolean).join('\n')).includes(qn);
  }

  // ---- записи в DOM без лишней перерисовки ---------------------------------------

  // Фокус на кнопке/плашке внутри перерисовываемой области -> после записи вернуть на «такую же»
  function captureFocus(){
    const a = document.activeElement;
    if(!a || a === document.body || !a.dataset || !a.dataset.action) return null;
    const scope = ['nl-main', DRAWER_ID].find(id => $(id) && $(id).contains(a));
    if(!scope) return null;
    const tr = a.closest('tr[data-screen-id]');
    return {scope, action: a.dataset.action, pid: a.dataset.playlistId, mid: a.dataset.mediaId,
            row: tr ? tr.dataset.screenId : null};
  }

  function restoreFocus(f){
    if(!f) return;
    let sel = `[data-action="${f.action}"]`;
    if(f.pid) sel += `[data-playlist-id="${f.pid}"]`;
    if(f.mid) sel += `[data-media-id="${f.mid}"]`;
    const scope = $(f.scope);
    const root = f.row ? scope && scope.querySelector(`tr[data-screen-id="${f.row}"]`) : scope;
    const el = root && root.querySelector(sel);
    if(el) el.focus({preventScroll: true});
  }

  function captureScroll(){
    const lists = {};
    document.querySelectorAll('#nl-root [data-sc]').forEach(el => { lists[el.dataset.sc] = [el.scrollLeft, el.scrollTop]; });
    const dr = $(DRAWER_ID);
    return {y: window.scrollY, x: window.scrollX, lists, drawer: dr ? dr.scrollTop : 0};
  }

  function restoreScroll(sc){
    document.querySelectorAll('#nl-root [data-sc]').forEach(el => {
      const p = sc.lists[el.dataset.sc];
      if(p){ el.scrollLeft = p[0]; el.scrollTop = p[1]; }
    });
    const dr = $(DRAWER_ID);
    if(dr) dr.scrollTop = sc.drawer;
    if(window.scrollY !== sc.y || window.scrollX !== sc.x) window.scrollTo(sc.x, sc.y);
  }

  // Записать разметку, только если она изменилась; прокрутка и фокус остаются на месте
  function put(el, key, html, st){
    if(!el || st.html[key] === html) return false;
    const sc = captureScroll(), f = captureFocus();
    st.html[key] = html;
    el.innerHTML = html;
    restoreScroll(sc);
    restoreFocus(f);
    return true;
  }

  // ---- таблица и шапка ------------------------------------------------------------

  function statHtml(st){
    const d = st.data;
    if(!d) return '';
    const c = d.counts;
    const found = st.query.trim() && st.shown != null ? `<span>найдено <b>${st.shown}</b></span>` : '';
    return `<span><b>${c.total}</b> ${plural(c.total, 'экран', 'экрана', 'экранов')}</span>
      <span><span class="nl-dot"></span><b>${c.online}</b> в сети</span>
      <span><span class="nl-dot off"></span><b>${c.offline}</b> не в сети</span>
      ${found}
      <span class="dim">обновлено ${esc(fmtClock(d.generated_at))}</span>`;
  }

  function errorHtml(st, withData){
    const when = withData ? ` Показаны данные на ${esc(fmtClock(st.data.generated_at))}.` : '';
    return `<div class="nl-err" role="alert"><span>Не удалось ${withData ? 'обновить данные' : 'загрузить данные'}: ${esc(st.error)}.${when}</span>
      <button type="button" class="btn" data-action="nl-retry">Повторить</button></div>`;
  }

  function mainHtml(st){
    const d = st.data;
    if(!d) return st.error ? errorHtml(st, false) : '<div class="empty">Загрузка…</div>';
    const banner = st.error ? errorHtml(st, true) : '';
    if(!d.screens.length){
      st.shown = null;
      return banner + '<div class="empty">В сети пока нет ни одного экрана. Добавьте экран в разделе «Экраны» — здесь появится, что на нём идёт.</div>';
    }
    const qn = norm(st.query.trim());
    const rows = d.screens.filter(s => matches(s, qn));
    st.shown = rows.length;
    if(!rows.length){
      return banner + `<div class="empty">Ничего не найдено по запросу «${esc(st.query.trim())}». Поиск идёт по названию экрана, адресу и названию плейлиста.</div>`;
    }
    const last = d.broadcast.is_on ? 'После эфира' : 'Дальше';
    return banner + `<div class="nl-tblwrap" data-sc="tbl"><table class="nl-tbl">
      <thead><tr><th>Экран</th><th>Сейчас идёт</th><th>Играет ролик</th><th>${last}</th></tr></thead>
      <tbody>${rows.map(s => rowHtml(s, st)).join('')}</tbody></table></div>`;
  }

  function renderMain(st){
    put($('nl-main'), 'main', mainHtml(st), st);
    $('nl-stat').innerHTML = statHtml(st);
  }

  // ---- панель «О плейлисте» ---------------------------------------------------------

  function screensNow(st, pid){
    if(!st.data) return [];
    return st.data.screens.filter(s => s.now.playlist_ids.includes(pid));
  }

  function playlistName(st, pid){
    const known = st.data && st.data.playlists[String(pid)];
    return (known && known.name) || st.pname || ('Плейлист #' + pid);
  }

  function partErr(r){
    return `<div class="nl-dim">Не удалось получить: ${esc(errText(r.reason))}</div>`;
  }

  function screensSection(st, pid){
    const list = screensNow(st, pid);
    if(!list.length) return '<div class="nl-lbl">Сейчас идёт</div><div class="nl-dim">Сейчас не идёт ни на одном экране</div>';
    const rows = list.map(s => {
      const marks = [];
      if(s.now.kind === 'alternate') marks.push('⇄ по очереди');
      if(s.now.kind === 'broadcast') marks.push('📡 общий эфир');
      if(!s.online) marks.push(fmtOfflineFor(s.offline_for_seconds));
      return `<div class="nl-row"><span class="nl-dot${s.online ? '' : ' off'}" role="img" aria-label="${s.online ? 'в сети' : 'не в сети'}"></span>
        <span class="nl-n">${esc(s.name)}${marks.length ? ` <span class="dim">${esc(marks.join(' · '))}</span>` : ''}</span></div>`;
    }).join('');
    return `<div class="nl-lbl">Сейчас идёт ${onScreens(list.length)}</div><div class="nl-scrl" data-sc="scr">${rows}</div>`;
  }

  function itemsSection(r){
    if(r.status !== 'fulfilled') return '<div class="nl-lbl">Ролики</div>' + partErr(r);
    const items = r.value || [];
    if(!items.length) return '<div class="nl-lbl">Ролики</div><div class="nl-dim">В плейлисте нет роликов</div>';
    const rows = items.map(it => {
      const name = it.title || it.filename || ('Ролик ' + it.media_id);
      return `<button type="button" class="nl-row nl-rowbtn" data-action="nl-preview-clip" data-media-id="${Number(it.media_id)}"
        title="Смотреть ролик">${thumbHtml(it.media_id)}<span class="nl-n">${esc(name)}</span>
        <span class="nl-rep">×${Number(it.repeat_count) || 1}</span>
        <span class="dim">${it.duration_seconds ? fmtDur(it.duration_seconds) : '—'}</span></button>`;
    }).join('');
    return `<div class="nl-lbl">Ролики</div><div class="nl-scrl" data-sc="items">${rows}</div>`;
  }

  const PLC_STATUS = {
    active:  ['идёт сейчас', 'now'],
    planned: ['запланирован', 'plan'],
    done:    ['закончился', 'done'],
    stopped: ['остановлен', 'stop'],
  };
  const PLC_SHOWN = 6;

  function placementRow(p){
    const st = PLC_STATUS[p.status] || [p.status, 'done'];
    let where;
    if(p.all_screens) where = 'Все экраны · ' + p.screen_count;
    else if(p.screens.length === 1) where = p.screens[0].name;
    else where = p.screens.length + ' ' + plural(p.screens.length, 'экран', 'экрана', 'экранов');
    const names = p.all_screens ? '' : p.screens.map(s => s.name).join(', ');
    return `<div class="nl-row nl-prow"><span class="nl-n" title="${esc(names)}">📅 ${esc(where)} · ${esc(dates(p.date_from, p.date_to))} · ${hh(p.hour_from)}–${hh(p.hour_to)} ч</span>
      <span class="plb-st ${st[1]}">${esc(st[0])}</span></div>`;
  }

  function whereSection(plc, usage){
    let h = '<div class="nl-lbl">Где и когда идёт</div>';
    if(plc.status !== 'fulfilled') h += partErr(plc);
    else{
      const rows = plc.value || [];
      if(!rows.length) h += '<div class="nl-dim">Показов по датам нет</div>';
      else{
        h += rows.slice(0, PLC_SHOWN).map(placementRow).join('');
        if(rows.length > PLC_SHOWN) h += `<div class="nl-dim">и ещё ${rows.length - PLC_SHOWN} ${plural(rows.length - PLC_SHOWN, 'показ', 'показа', 'показов')}</div>`;
      }
    }
    if(usage.status === 'fulfilled' && usage.value && usage.value.template_slots > 0){
      const n = usage.value.template_slots;
      h += `<div class="nl-row nl-prow"><span class="nl-n">в недельном расписании: ${n} ${plural(n, 'слот', 'слота', 'слотов')}</span></div>`;
    }
    return h;
  }

  function subLine(det){
    if(!det) return '';
    const parts = [];
    if(det.items.status === 'fulfilled'){
      const items = det.items.value || [];
      const plays = items.reduce((a, it) => a + (Number(it.repeat_count) || 1), 0);
      const secs = items.reduce((a, it) => a + (Number(it.duration_seconds) || 0) * (Number(it.repeat_count) || 1), 0);
      parts.push(`${items.length} ${plural(items.length, 'ролик', 'ролика', 'роликов')}`,
        `${plays} ${plural(plays, 'показ', 'показа', 'показов')}`, fmtDur(secs));
    }
    if(det.fill.status === 'fulfilled'){
      const f = det.fill.value;
      parts.push(f.fill_to_hour ? `блок ${Math.round((f.target_seconds || 3600) / 60)} мин с заглушками` : 'как есть');
    }
    return parts.join(' · ');
  }

  // Тело панели (под заголовком): перерисовывается при каждом обновлении данных
  function drawerBodyHtml(st){
    const pid = st.pid, det = st.detail;
    let h = `<div class="nl-sub">${det ? esc(subLine(det)) || '&nbsp;' : 'Загрузка…'}</div>`;
    h += screensSection(st, pid);
    if(det){
      h += itemsSection(det.items);
      h += whereSection(det.plc, det.usage);
    }
    if(canWrite()){
      h += `<div class="nl-acts">
        <button type="button" class="btn" data-action="nl-edit-playlist" data-playlist-id="${pid}">✎ Изменить плейлист</button>
        <button type="button" class="btn primary" data-action="nl-send-playlist" data-playlist-id="${pid}">📤 Отправить на экраны</button>
      </div>`;
    }
    return h;
  }

  // Заголовок с «✕» создаётся один раз при открытии и не перерисовывается: клик по «✕»
  // не должен промахнуться из-за обновления, пришедшего между нажатием и отпусканием.
  function renderDrawer(st){
    const box = $(DRAWER_ID), lay = $('nl-lay');
    if(!box || !lay) return;
    const open = st.pid != null;
    lay.classList.toggle('has-drawer', open);
    box.hidden = !open;
    if(!open){ st.html.drawer = null; box.innerHTML = ''; return; }
    if(!$('nl-drawer-title')){
      box.innerHTML = `<div class="nl-dh"><h3 id="nl-drawer-title" tabindex="-1"></h3>
        <button type="button" class="nl-x" data-action="nl-close-drawer" aria-label="Закрыть панель">✕</button></div>
        <div id="nl-drawer-body"></div>`;
      st.html.drawer = null;
    }
    const title = $('nl-drawer-title'), name = playlistName(st, st.pid);
    if(title.textContent !== name) title.textContent = name;
    put($('nl-drawer-body'), 'drawer', drawerBodyHtml(st), st);
  }

  // Подробности плейлиста: ролики, режим блока, показы, слоты шаблона. Каждый запрос
  // сам по себе — сбой одного не гасит остальные панели.
  async function loadDetail(st, pid){
    const seq = ++st.detailSeq;
    const [items, fill, plc, usage] = await Promise.allSettled([
      api('/playlists/' + pid + '/items'),
      api('/playlists/' + pid + '/fill_info'),
      api('/placements?playlist_id=' + pid),
      api('/network/playlist/' + pid + '/usage'),
    ]);
    if(!alive(st) || st.pid !== pid || seq !== st.detailSeq) return;
    const gone = r => r.status === 'rejected' && /не найден/i.test(errText(r.reason));
    if(gone(fill) && gone(usage)){            // плейлист удалили, пока панель была открыта
      closeDrawer();
      toast('Плейлист больше не существует');
      return;
    }
    st.detail = {items, fill, plc, usage};
    renderDrawer(st);
  }

  function openDrawer(pid, opener){
    const st = S;
    if(!st || !alive(st) || !pid) return;
    const same = st.pid === pid;
    st.pid = pid;
    if(!same){
      st.detail = null;
      st.pname = ((st.data && st.data.playlists[String(pid)]) || {}).name
        || (opener && opener.dataset.playlistName) || '';
    }
    renderMain(st);                           // подсветить плашки этого плейлиста
    renderDrawer(st);
    if(!same){
      const box = $(DRAWER_ID);
      if(box) box.scrollTop = 0;
      loadDetail(st, pid);
      const t = $('nl-drawer-title');
      if(t) t.focus({preventScroll: true});
    }
  }

  function closeDrawer(){
    const st = S;
    if(!st || st.pid == null) return;
    const pid = st.pid;
    st.pid = null;
    st.detail = null;
    st.detailSeq++;
    renderDrawer(st);
    renderMain(st);
    const back = document.querySelector(`#nl-main [data-action="nl-open-playlist"][data-playlist-id="${pid}"]`);
    if(back) back.focus({preventScroll: true});
  }

  // ---- загрузка ----------------------------------------------------------------------

  function load(st){
    const p = (async () => {
      try{
        const d = await api('/network/live');
        if(!alive(st)) return;
        st.data = d;
        st.error = null;
        st.at = Date.now();
        try{
          if(typeof Signage.renderBroadcastBar === 'function') Signage.renderBroadcastBar(d);
        }catch(e){ console.error('renderBroadcastBar:', e); }
        if(st.pid != null) loadDetail(st, st.pid);
      }catch(e){
        if(!alive(st) || String(e && e.message) === 'unauthorized') return;
        st.error = errText(e);
      }
      if(alive(st)){ renderMain(st); renderDrawer(st); }
    })().finally(() => {
      st.loading = null;
      if(st.again){ st.again = false; load(st); }
    });
    st.loading = p;
    return p;
  }

  // Перечитать сейчас; если запрос уже идёт — после него ещё один (он мог выйти до правки)
  function refresh(st){
    if(!st || S !== st || !alive(st)) return Promise.resolve();
    if(st.loading){ st.again = true; return st.loading; }
    return load(st);
  }

  function tick(st){
    if(!alive(st)){ stop(st); return; }
    if(document.visibilityState !== 'visible') return;
    refresh(st);
  }

  function stop(st){
    if(st && st.timer){ clearInterval(st.timer); st.timer = 0; }
  }

  function stopNetworkLive(){
    stop(S);
    S = null;
  }

  // ---- действия -------------------------------------------------------------------------

  function editPlaylist(){
    const st = S;
    if(!st || st.pid == null || !canWrite()) return;
    const pid = st.pid;
    const n = screensNow(st, pid).length;
    const notice = n > 0
      ? `Плейлист сейчас идёт ${onScreens(n)} — изменения дойдут до ${n % 10 === 1 && n % 100 !== 11 ? 'него' : 'них'} в течение минуты`
      : null;
    Signage.openPlaylistEditor({
      playlistId: pid, name: playlistName(st, pid), notice,
      onSaved(r){
        if(S === st && st.pid === pid && r && r.name) st.pname = r.name;
        refresh(st);
      },
    });
  }

  function sendPlaylist(){
    const st = S;
    if(!st || st.pid == null || !canWrite()) return;
    Signage.openPlacementEditor({playlistId: st.pid, name: playlistName(st, st.pid)});
  }

  function retry(){
    const st = S;
    if(!st) return;
    if(!st.data){ st.error = null; renderMain(st); }
    refresh(st);
  }

  // ---- вход в раздел ------------------------------------------------------------------------

  async function viewNetworkLive(){
    stop(S);
    const view = $('view');
    const st = S = {
      data: null, error: null, query: '', shown: null,
      pid: null, pname: '', detail: null, detailSeq: 0,
      loading: null, again: false, timer: 0, at: 0, html: {},
    };
    view.innerHTML = `<div id="nl-root" class="nl">
      <div class="nl-head">
        <div class="nl-stat" id="nl-stat"></div>
        <input class="inp nl-search" id="nl-search" type="text" autocomplete="off"
          placeholder="🔍 экран, адрес или плейлист" aria-label="Поиск по экрану, адресу и названию плейлиста">
      </div>
      <div id="nl-broadcast"></div>
      <div class="nl-lay" id="nl-lay">
        <div class="nl-main" id="nl-main"><div class="empty">Загрузка…</div></div>
        <aside class="nl-drawer" id="${DRAWER_ID}" aria-labelledby="nl-drawer-title" hidden></aside>
      </div>
    </div>`;
    st.timer = setInterval(() => tick(st), REFRESH_MS);
    await load(st);
  }

  function refreshNetworkLive(){
    return S ? refresh(S) : Promise.resolve();
  }

  // ---- события (по одному обработчику на документ) -------------------------------------------

  function initNetworkLiveActions(){
    if(window.__networkLiveActionsInitialized) return;
    window.__networkLiveActionsInitialized = true;

    document.addEventListener('click', e => {
      const el = e.target.closest('[data-action]');
      if(!el) return;
      const action = el.dataset.action;
      if(!action || !action.startsWith('nl-') || !S) return;

      switch(action){
        case 'nl-open-playlist': return openDrawer(Number(el.dataset.playlistId), el);
        case 'nl-close-drawer':  return closeDrawer();
        case 'nl-edit-playlist': return editPlaylist();
        case 'nl-send-playlist': return sendPlaylist();
        case 'nl-preview-clip':  return Signage.openMediaPreview(Number(el.dataset.mediaId));
        case 'nl-retry':         return retry();
      }
    });

    document.addEventListener('input', e => {
      if(!S || !e.target || e.target.id !== 'nl-search') return;
      S.query = e.target.value;
      renderMain(S);
    });

    // Esc закрывает панель; пока открыто окно, Esc принадлежит ему
    document.addEventListener('keydown', e => {
      if(e.key !== 'Escape' || e.defaultPrevented || e.isComposing) return;
      if(!S || S.pid == null || document.body.classList.contains('modal-open')) return;
      closeDrawer();
    });

    // Вернулись на вкладку после долгого отсутствия — не ждём очередного тика
    document.addEventListener('visibilitychange', () => {
      if(document.visibilityState !== 'visible' || !S || !alive(S)) return;
      if(Date.now() - S.at >= REFRESH_MS) refresh(S);
    });

    // Показ создан или изменён в окне «Куда и когда» — таблица и панель обновляются сразу
    document.addEventListener('placements-changed', () => { if(S && alive(S)) refresh(S); });
  }

  window.Signage = window.Signage || {};
  window.Signage.viewNetworkLive = viewNetworkLive;
  window.Signage.refreshNetworkLive = refreshNetworkLive;
  window.Signage.stopNetworkLive = stopNetworkLive;

  initNetworkLiveActions();
})();
