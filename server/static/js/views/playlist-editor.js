//=============================================================================
// ОКНО «ПЛЕЙЛИСТ»: новый и правка (шаг 1 «Ролики»)
//
//   Signage.openPlaylistEditor({playlistId?, name?, onSaved?, notice?})
//     без playlistId — новый плейлист (POST /playlists/full);
//     с playlistId   — правка: окно открывается с уже отмеченными роликами в их
//                      порядке и с повторами, сохранение — PUT /playlists/{id}/items;
//     name      — название плейлиста (для правки; нет — берётся из /playlists);
//     onSaved   — вызывается после сохранения: ({id, name, created});
//     notice    — текст-подсказка серой строкой над плитками (например, «Плейлист
//                 сейчас идёт на 2 экранах — …» из «Эфира сети»); нет — строки нет.
//
// Слева папки (Signage.buildMediaGroups из playlists.js), сверху поиск по названию
// по ВСЕМ папкам сразу («ё» = «е», регистр не важен), в центре плитки роликов.
// Клик по плитке — галочка; цифра в галочке — порядок в плейлисте (порядок
// отметок), снятие галочки сдвигает номера. У отмеченного — счётчик «− ×N +»
// (1…99). Не одобренные ролики — серые, с подписью статуса, не выбираются;
// заглушки и документы не показываются. Через 300 мс после изменения
// POST /playlists/check отдаёт мягкие предупреждения (повторы, доля, дубли):
// они только предупреждают, кнопка меняется на «…всё равно».
// «Создать и отправить →» открывает Signage.openPlacementEditor({playlistId})
// (шаг 2, отдельная задача); пока функции нет — кнопка не показывается.
//=============================================================================
(function(){
  const MAX_REPEAT = 99;      // как на сервере (routers/playlists.py MAX_REPEAT)
  const MAX_NAME = 128;       // playlists.name VARCHAR(128)
  const CHECK_DELAY = 300;    // мс после последнего изменения до /playlists/check
  const SEARCH_DELAY = 150;   // мс после последней буквы до перерисовки плиток

  // Статус модерации → подпись на плитке (как в просмотре ролика)
  const STATUS_RU = {pending: 'на модерации', rejected: 'отклонён'};

  let S = null;               // состояние открытого окна (оно может быть только одно)

  // ---- мелочи ---------------------------------------------------------------

  function plural(n, one, few, many){
    const m10 = n % 10, m100 = n % 100;
    if(m10 === 1 && m100 !== 11) return one;
    if(m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }

  function fmtDur(sec){
    sec = Math.round(Number(sec) || 0);
    if(sec <= 0) return '0:00';
    const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    const pad = n => String(n).padStart(2, '0');
    return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  }

  // Поиск без учёта регистра и «ё» = «е»: «пятерочка» находит «Пятёрочка»
  function norm(s){
    return String(s == null ? '' : s).toLowerCase().replace(/ё/g, 'е');
  }

  function clipName(m){ return m.title || m.filename || ('Ролик ' + m.id); }
  function isApproved(m){ return m.review_status === 'approved'; }

  // api() бросает Error(тело ответа); у FastAPI 400/404/409 — {"detail": "текст"}
  function errText(e){
    const t = String((e && e.message) || e || '');
    try{
      const j = JSON.parse(t);
      if(j && typeof j.detail === 'string') return j.detail;
      if(j && Array.isArray(j.detail)) return 'Проверьте название и список роликов';
    }catch(_){}
    return t;
  }

  // Название с подсветкой совпадения поиска (qn — уже нормализованный запрос)
  function highlight(name, qn){
    if(!qn) return esc(name);
    const n = norm(name);
    if(n.length !== name.length) return esc(name);   // экзотический регистр сдвигает индексы
    let out = '', pos = 0, i;
    while((i = n.indexOf(qn, pos)) >= 0){
      out += esc(name.slice(pos, i)) + '<mark>' + esc(name.slice(i, i + qn.length)) + '</mark>';
      pos = i + qn.length;
    }
    return out + esc(name.slice(pos));
  }

  const $ = id => document.getElementById(id);

  // ---- плитки -----------------------------------------------------------------

  function tileHtml(m, qn){
    const id = m.id;
    const name = clipName(m);
    const approved = isApproved(m);
    const status = approved ? '' : (STATUS_RU[m.review_status] || 'не одобрен');
    const dups = S.dups[id] || [];
    const dupBadge = dups.length ? '<span class="pled-badge pled-dup">дубль</span>' : '';
    const dupLine = dups.length
      ? `<i class="pled-dupline">тот же файл, что „${esc(dups[0].title)}“${dups.length > 1 ? ' и ещё ' + (dups.length - 1) : ''}</i>`
      : '';
    return `<div class="pled-tile${approved ? '' : ' off'}" data-media-id="${id}">
      <div class="pled-pick" data-action="pled-toggle" data-media-id="${id}">
        <div class="pled-thumb">
          <div class="pled-th">▶<img src="${API}/media/${id}/thumbnail" alt="" loading="lazy" onerror="this.remove()"></div>
          <button type="button" class="pled-chk" role="checkbox" aria-checked="false"
            aria-label="${esc(name)}" data-action="pled-toggle" data-media-id="${id}"></button>
          <button type="button" class="pled-watch" data-action="mprev-open" data-media-id="${id}"
            aria-label="Смотреть: ${esc(name)}">▶ смотреть</button>
          ${dupBadge}
          ${status ? `<span class="pled-badge pled-status${m.review_status === 'rejected' ? ' rej' : ''}">${esc(status)}</span>` : ''}
        </div>
        <div class="pled-tt"><span class="pled-nm">${highlight(name, qn)}</span><i>${m.duration_seconds ? fmtDur(m.duration_seconds) : '—'}</i>${dupLine}</div>
      </div>
      <div class="pled-rep" hidden>
        <button type="button" data-action="pled-rep-minus" data-media-id="${id}" aria-label="Меньше повторов">−</button>
        <b aria-live="polite">×1</b>
        <button type="button" data-action="pled-rep-plus" data-media-id="${id}" aria-label="Больше повторов">+</button>
      </div>
    </div>`;
  }

  // Отметки, номера, повторы и жёлтая рамка («в предупреждении») — на готовых плитках,
  // без пересборки: фокус и загруженные превью остаются на месте.
  function refreshTiles(){
    const items = new Map(S.sel.map((x, i) => [x.id, {rep: x.rep, n: i + 1}]));
    const warnText = S.warnings.join('\n');
    S.grid.querySelectorAll('.pled-tile').forEach(t => {
      const id = Number(t.dataset.mediaId);
      const m = S.byId.get(id);
      const it = items.get(id);
      t.classList.toggle('sel', !!it);
      t.classList.toggle('over', !!it && warnText.includes('«' + clipName(m) + '»'));
      const chk = t.querySelector('.pled-chk');
      chk.setAttribute('aria-checked', it ? 'true' : 'false');
      chk.textContent = it ? String(it.n) : '';
      const rep = t.querySelector('.pled-rep');
      rep.hidden = !it;
      if(it) rep.querySelector('b').textContent = '×' + it.rep;
    });
  }

  function currentList(){
    const qn = norm(S.query.trim());
    if(qn) return S.clips.filter(m => norm(clipName(m)).includes(qn));
    return Signage.filterMediaByGroup(S.clips, S.folderKey);
  }

  function renderGrid(){
    const qn = norm(S.query.trim());
    const list = currentList();
    if(list.length){
      S.grid.innerHTML = list.map(m => tileHtml(m, qn)).join('');
    }else if(qn){
      S.grid.innerHTML = '<div class="pled-empty">Ничего не найдено. Поиск идёт по названию во всех папках.</div>';
    }else if(!S.clips.length){
      S.grid.innerHTML = '<div class="pled-empty">В медиатеке нет роликов для плейлиста. Загрузите их в разделе «Медиатека».</div>';
    }else{
      S.grid.innerHTML = '<div class="pled-empty">В этой папке нет подходящих роликов.</div>';
    }
    S.found.textContent = qn && list.length ? `найдено ${list.length} · поиск по названию во всех папках` : '';
    S.side.classList.toggle('searching', !!qn);
    S.grid.scrollTop = 0;
    refreshTiles();
  }

  function renderSide(){
    S.side.innerHTML = S.groups.map(g => {
      const cnt = Signage.filterMediaByGroup(S.clips, g.key).length;
      return `<button type="button" class="pled-folder${g.key === S.folderKey ? ' on' : ''}"
        data-action="pled-folder" data-key="${esc(g.key)}"><span>${esc(g.label)}</span><span class="pled-cnt">${cnt}</span></button>`;
    }).join('');
  }

  // ---- итоги, предупреждения, кнопки ------------------------------------------

  function refreshSummary(){
    let plays = 0, secs = 0;
    S.sel.forEach(x => {
      plays += x.rep;
      secs += (Number(S.byId.get(x.id).duration_seconds) || 0) * x.rep;
    });
    const n = S.sel.length;
    S.sum.textContent = `Выбрано ${n} ${plural(n, 'ролик', 'ролика', 'роликов')} · `
      + `${plays} ${plural(plays, 'показ', 'показа', 'показов')} · ${fmtDur(secs)}`;
  }

  function refreshButtons(){
    const base = S.playlistId ? 'Сохранить' : 'Создать';
    const label = S.warnings.length ? base + ' всё равно' : base;
    S.btnSave.textContent = label;
    S.btnSave.disabled = S.busy || !S.loaded;
    if(S.btnSend){
      S.btnSend.disabled = S.busy || !S.loaded;
    }
  }

  function setWarnings(list){
    S.warnings = list;
    if(list.length){
      S.warnBox.innerHTML = list.map(w => `<div>⚠ ${esc(w)}</div>`).join('');
      S.warnBox.hidden = false;
    }else{
      S.warnBox.hidden = true;
      S.warnBox.innerHTML = '';
    }
    refreshButtons();
    refreshTiles();
  }

  function showError(text){
    S.errBox.textContent = text || '';
    S.errBox.hidden = !text;
    // Ошибка сервера в нижней части окна — не прячем за прокруткой
    if(text) S.errBox.scrollIntoView({block: 'nearest'});
  }

  function itemsBody(st){
    return st.sel.map(x => ({media_id: x.id, repeat_count: x.rep}));
  }

  function scheduleCheck(){
    clearTimeout(S.checkTimer);
    if(!S.sel.length){
      S.checkSeq++;               // ответ на прошлый запрос уже не нужен
      setWarnings([]);
      return;
    }
    const st = S;
    st.checkTimer = setTimeout(() => runCheck(st), CHECK_DELAY);
  }

  async function runCheck(st){
    const seq = ++st.checkSeq;
    let warnings = [];
    try{
      const r = await api('/playlists/check', {method: 'POST', body: JSON.stringify({items: itemsBody(st)})});
      warnings = (r && r.warnings) || [];
    }catch(_){
      // Проверка — только подсказка; причину отказа покажет сохранение
    }
    if(st.closed || seq !== st.checkSeq) return;
    setWarnings(warnings);
  }

  // ---- действия ---------------------------------------------------------------

  function onChange(){
    if(S.errBox && !S.errBox.hidden) showError('');
    refreshTiles();
    refreshSummary();
    scheduleCheck();
  }

  function toggle(id){
    const i = S.sel.findIndex(x => x.id === id);
    if(i >= 0){
      S.sel.splice(i, 1);
    }else{
      const m = S.byId.get(id);
      if(!m || !isApproved(m)) return;       // не одобренный ролик выбрать нельзя
      S.sel.push({id, rep: 1});
    }
    onChange();
  }

  function changeRep(id, d){
    const it = S.sel.find(x => x.id === id);
    if(!it) return;
    const rep = Math.min(MAX_REPEAT, Math.max(1, it.rep + d));
    if(rep === it.rep) return;
    it.rep = rep;
    onChange();
  }

  function selectFolder(key){
    S.query = '';
    S.search.value = '';
    S.folderKey = key;
    renderSide();
    renderGrid();
  }

  function snapshot(){
    return JSON.stringify([S.nameInp.value.trim(), S.sel]);
  }

  function isDirty(){
    return !!(S && S.loaded && !S.busy && snapshot() !== S.initial);
  }

  // Закрытие по «Отмена», ✕, клику по фону и Esc: если есть несохранённое — спрашиваем
  function requestClose(){
    if(!S) return;
    if(isDirty() && !confirm('Закрыть окно? Название и выбранные ролики не сохранятся.')) return;
    S.modal.close();
  }

  async function save(sendAfter){
    const st = S;
    if(!st || st.busy || !st.loaded) return;
    const name = st.nameInp.value.trim();
    if(!name){
      showError('Введите название плейлиста');
      st.nameInp.focus();
      return;
    }
    if(!st.sel.length){
      showError('Отметьте хотя бы один ролик');
      return;
    }
    showError('');
    st.busy = true;
    refreshButtons();
    const body = JSON.stringify({name, items: itemsBody(st)});
    let id, warnings;
    try{
      if(st.playlistId){
        const r = await api('/playlists/' + st.playlistId + '/items', {method: 'PUT', body});
        id = st.playlistId;
        warnings = r.warnings;
      }else{
        const r = await api('/playlists/full', {method: 'POST', body});
        id = r.id;
        warnings = r.warnings;
      }
    }catch(e){
      if(String(e && e.message) === 'unauthorized'){ if(!st.closed) st.modal.close(); return; }
      if(st.closed) return;
      st.busy = false;
      refreshButtons();
      showError(errText(e));            // 400/404/409 приходят русским текстом
      return;
    }
    toast((st.playlistId ? 'Плейлист сохранён' : 'Плейлист создан')
      + (warnings && warnings.length ? ' (с предупреждениями)' : ''));
    if(!st.closed) st.modal.close();
    try{
      if(st.onSaved) st.onSaved({id, name, created: !st.playlistId});
    }catch(e){ console.error('playlist-editor onSaved:', e); }
    if(sendAfter && typeof Signage.openPlacementEditor === 'function'){
      Signage.openPlacementEditor({playlistId: id});
    }
  }

  // ---- открытие окна ------------------------------------------------------------

  async function openPlaylistEditor(opts){
    opts = opts || {};
    if(!canWrite()){ toast('Нет прав на изменение плейлистов'); return; }
    if(S) return;                                  // окно уже открыто (двойной клик)
    const playlistId = Number(opts.playlistId) || 0;
    const canSend = !playlistId && typeof Signage.openPlacementEditor === 'function';
    const st = S = {
      playlistId, onSaved: opts.onSaved,
      closed: false, loaded: false, busy: false,
      sel: [], warnings: [], checkSeq: 0, checkTimer: 0, searchTimer: 0,
      query: '', folderKey: 'all', initial: '',
    };
    const notice = opts.notice ? String(opts.notice) : '';

    const modal = openModal({
      title: playlistId ? 'Изменить плейлист' : 'Новый плейлист',
      body: '<div class="empty">Загрузка…</div>',
      footer: `<span class="pled-sum" id="pled-sum" aria-live="polite"></span>
        <button type="button" class="btn" data-action="pled-cancel">Отмена</button>
        <button type="button" class="btn${canSend ? '' : ' primary'}" id="pled-save" data-action="pled-save" disabled>Создать</button>
        ${canSend ? '<button type="button" class="btn primary" id="pled-save-send" data-action="pled-save-send" disabled>Создать и отправить →</button>' : ''}`,
      width: 1000,
      className: 'modal-pled',
      onClose(){
        st.closed = true;
        clearTimeout(st.checkTimer);
        clearTimeout(st.searchTimer);
        if(S === st) S = null;
      },
    });
    st.modal = modal;
    st.btnSave = $('pled-save');
    st.btnSend = $('pled-save-send');
    st.sum = $('pled-sum');

    // Несохранённый выбор не теряем от случайного клика по фону, ✕ или Esc.
    // Перехватываем раньше штатного закрытия из modal.js (оно ничего не знает про выбор).
    const backdrop = modal.el.parentElement;
    let downOnBackdrop = true;
    backdrop.addEventListener('mousedown', e => { downOnBackdrop = (e.target === backdrop); }, true);
    backdrop.addEventListener('click', e => {
      const onBackdrop = (e.target === backdrop) && downOnBackdrop;
      downOnBackdrop = true;
      const x = e.target.closest && e.target.closest('[data-modal-close]');
      if(!onBackdrop && !(x && modal.el.contains(x))) return;
      if(!isDirty()) return;                       // нечего терять — штатное закрытие
      e.stopImmediatePropagation();
      requestClose();
    }, true);
    modal.el.addEventListener('keydown', e => {
      if(e.key !== 'Escape' || e.defaultPrevented || e.isComposing || !isDirty()) return;
      e.preventDefault();                          // modal.js при preventDefault Esc не трогает
      requestClose();
    });

    // Данные: ролики, папки, дубли; при правке — состав плейлиста и название
    let media, folders, dups, items = [], plName = String(opts.name || '');
    try{
      [media, folders, dups, items, plName] = await Promise.all([
        api('/media'),
        api('/media/folders-all').catch(() => []),
        api('/media/duplicates').catch(() => ({})),
        playlistId ? api('/playlists/' + playlistId + '/items') : [],
        playlistId && opts.name == null
          ? api('/playlists').then(l => ((l || []).find(p => p.id === playlistId) || {}).name || '')
          : plName,
      ]);
    }catch(e){
      if(st.closed) return;
      modal.el.querySelector('.modal-body').innerHTML =
        `<div class="empty">Не удалось загрузить данные: ${esc(errText(e))}</div>`;
      return;
    }
    if(st.closed) return;

    // Заглушки добавляются в эфирный блок сами, документы в плейлист не идут
    st.clips = media.filter(m => !m.is_filler && m.status !== 'document');
    st.byId = new Map(st.clips.map(m => [m.id, m]));
    st.dups = dups || {};
    st.groups = Signage.buildMediaGroups(st.clips, folders);
    let skipped = 0;
    items.forEach(it => {
      if(st.byId.has(it.media_id)) st.sel.push({id: it.media_id, rep: it.repeat_count});
      else skipped++;                              // заглушка — в этом окне её не показываем
    });

    modal.el.querySelector('.modal-body').innerHTML = `
      <div class="pled-name">
        <label for="pled-name">Название</label>
        <input class="inp" id="pled-name" maxlength="${MAX_NAME}" autocomplete="off"
          placeholder="Например: Утро будни — ТЦ «Галерея»">
      </div>
      ${notice ? `<div class="pled-info" role="note"><span aria-hidden="true">ℹ</span> ${esc(notice)}</div>` : ''}
      <div class="pled-main">
        <div class="pled-side" id="pled-side"></div>
        <div class="pled-right">
          <div class="pled-search">
            <input class="inp" id="pled-search" autocomplete="off" aria-label="Поиск ролика по названию"
              placeholder="🔍 Найти ролик по названию">
            <span class="pled-found" id="pled-found" aria-live="polite"></span>
          </div>
          <div class="pled-grid" id="pled-grid"></div>
        </div>
      </div>
      ${skipped ? `<div class="pled-note">В плейлисте есть ролики, которых нет в этом окне (заглушки): ${skipped}. При сохранении они будут убраны.</div>` : ''}
      <div class="pled-warns" id="pled-warns" role="status" hidden></div>
      <div class="pled-err" id="pled-err" role="alert" hidden></div>`;
    st.nameInp = $('pled-name');
    st.search = $('pled-search');
    st.found = $('pled-found');
    st.side = $('pled-side');
    st.grid = $('pled-grid');
    st.warnBox = $('pled-warns');
    st.errBox = $('pled-err');
    st.nameInp.value = plName || '';
    st.loaded = true;
    st.initial = snapshot();

    renderSide();
    renderGrid();
    refreshSummary();
    refreshButtons();
    if(st.sel.length) scheduleCheck();             // предупреждения уже готового плейлиста
    (playlistId ? st.search : st.nameInp).focus();
  }

  // ---- события (один обработчик на документ) -------------------------------------

  function initPlaylistEditorActions(){
    if(window.__playlistEditorActionsInitialized) return;
    window.__playlistEditorActionsInitialized = true;

    document.addEventListener('click', e => {
      const el = e.target.closest('[data-action]');
      if(!el) return;
      const action = el.dataset.action;
      if(!action || !action.startsWith('pled-') || !S) return;

      switch(action){
        case 'pled-toggle':     return S.loaded && toggle(Number(el.dataset.mediaId));
        case 'pled-rep-minus':  return changeRep(Number(el.dataset.mediaId), -1);
        case 'pled-rep-plus':   return changeRep(Number(el.dataset.mediaId), +1);
        case 'pled-folder':     return selectFolder(el.dataset.key);
        case 'pled-cancel':     return requestClose();
        case 'pled-save':       return save(false);
        case 'pled-save-send':  return save(true);
      }
    });

    document.addEventListener('input', e => {
      if(!S || !S.loaded || !e.target) return;
      if(e.target.id === 'pled-search'){
        const st = S;
        clearTimeout(st.searchTimer);
        st.searchTimer = setTimeout(() => {
          if(st.closed) return;
          st.query = st.search.value;
          renderGrid();
        }, SEARCH_DELAY);
      }else if(e.target.id === 'pled-name'){
        if(!S.errBox.hidden) showError('');
      }
    });
  }

  window.Signage = window.Signage || {};
  window.Signage.openPlaylistEditor = openPlaylistEditor;

  initPlaylistEditorActions();
})();
