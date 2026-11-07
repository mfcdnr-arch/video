//=============================================================================
// КОПИЯ НА ФЛЕШКУ: напоминание на дашборде, блок в «Настройках», окна
//
//   loadOffsiteBanner() — нарисовать напоминание в <div id="offsite-banner"> (его ставит
//                         dashboard.js самым верхним блоком; зовётся после каждой отрисовки
//                         дашборда);
//   loadOffsiteBlock()  — нарисовать блок «Копия на флешку» в <div id="offsite-block"> (его
//                         ставит settings.js над «Резервным копированием»).
//
// Всё — только для суперадмина: у остальных запрос не уходит, контейнера нет. Данные —
// GET /offsite/status. Не получилось (сервер старый, нет связи) — напоминания нет, в Настройках
// серая строка «Не удалось загрузить состояние копии»; в консоль ничего страшного не пишем.
//
// Напоминание. Жёлтое (.banner.warn): «Сегодня копию ещё не скачивали» или, если копия сегодня есть,
// а ролики новые, — «Копия сегодня скачана, а новые ролики — нет»; строка «Последняя — вчера, 18:42
// (admin) · новых роликов с тех пор: 3, 640,0 МБ»; кнопка «⤓ Скачать копию» (нет пароля на копию —
// «Задать пароль на копию») или «⤓ Скачать новые ролики». Зелёное (.banner.on): «Сегодня скачана в
// 09:15 (admin)» · «Новые ролики тоже скачаны», без кнопки. Последний ответ запоминается, и новая
// отрисовка дашборда рисует напоминание из него сразу, до ответа сервера: мигания нет, а запрос —
// один на отрисовку.
//
// Блок. Карточка «Ежедневная копия» (что внутри, последнее скачивание, пароль на копию, кнопка
// «⤓ Скачать копию · ≈ N МБ» или «Задать пароль на копию») и карточка «Ролики» («⤓ Скачать новые»,
// «⤓ Скачать все»).
//
// Окна (openModal): «Пароль на копию» (POST /offsite/password) и «Скачать копию на флешку»
// (POST /offsite/copy; сборка идёт минуты — пока ждём, кнопка «Собираю копию…», окно не закрывается).
// Ошибки сервера (неверный пароль и т.п.) показываются внутри окна. Ролики качаются без окна
// (POST /offsite/media), «Скачать все» — после confirm с числом и размером.
//
// Скачивание. В ответе приходит одноразовая ссылка /api/offsite/download?t=… — её открывает созданный
// <a href download> + click(): браузер качает прямо на диск, со своим индикатором. Это нарочно НЕ
// apiDownload (blob держал бы гигабайты в памяти вкладки). Сервер отмечает «скачано», только когда файл
// отдан целиком, поэтому после начала скачивания блок и напоминание перечитываются (через 3 с, затем
// раз в 15 с, не дольше часа), пока отметка не появится; кнопки роликов всё это время заняты.
//
// Время от сервера — наивное МСК («2026-11-04T18:42:00»): ЧЧ:ММ и дата берутся срезом строки.
// fmtServerTS тут не годится — он считает строку временем UTC и сдвигает на пояс браузера (+3 ч).
// «Сегодня» / «вчера» — по московской дате (МСК = UTC+3 круглый год).
//=============================================================================
(function(){
  const STATUS = '/offsite/status';
  const DL_PREFIX = '/api/offsite/download?t=';      // как отдаёт сервер; на другой адрес не переходим
  const POLL_FIRST_MS = 3000;                        // первое перечитывание после начала скачивания: малая копия уже могла дойти
  const POLL_MS = 15000;                             // дальше — раз в 15 с, пока файл не скачается целиком
  const POLL_CAP_MS = 60 * 60 * 1000;                // но не дольше часа (оборванное скачивание сервер не отметит никогда)
  const MSG_NOT_MOUNTED = 'Сервер не обновлён для копии на флешку: запустите install_server.sh';
  const MSG_NO_STATE = 'Состояние копии не загружено — обновите страницу';
  const MSG_STARTED = 'Скачивание началось. Отметка появится, когда файл скачается целиком';
  const MSG_PW_WARN = 'Запишите и храните отдельно от флешки. Без него копию не откроет никто, в том числе вы. '
    + 'Старые копии открываются старым паролем.';

  let S = null;                  // последний удачный ответ /offsite/status
  let SUser = null;              // под чьим входом он получен (смена пользователя сбрасывает кеш)
  let bannerSeq = 0;             // номер последнего запроса напоминания (устаревшие ответы отбрасываем)
  let blockSeq = 0;              // то же для блока в Настройках
  let blockState = 'loading';    // 'loading' | 'ok' | 'fail'
  let bannerBox = null, bannerHtml = '';   // что и куда нарисовано (то же самое не перерисовываем)
  let blockBox = null, blockHtml = '';
  let mediaBusy = false;         // идёт запрос ссылки на ролики или ролики ещё скачиваются (ждём отметку сервера)
  let watch = {};                // за чем следим: {copy|media: {from: last_at на момент клика, until: мс}}
  let watchUser = null;          // чей это вход (смена пользователя — слежение снимается)
  let pollTimer = 0;             // таймер единственного цикла опроса (0 — не взведён)
  let W = null;                  // открытое окно (оно может быть только одно)

  const $ = id => document.getElementById(id);

  // ---- мелочи ---------------------------------------------------------------

  // api() бросает Error(тело ответа); у FastAPI 400/403/409/503 — {"detail": "текст"}.
  // Всё видимое в панели — по-русски: текст сервера без единой русской буквы («Internal Server Error»,
  // английская ошибка сборки) заменяем общим сообщением, код берём из «HTTP 502», если он известен.
  function errText(e){
    if(e instanceof TypeError) return 'нет связи с сервером';
    const t = String((e && e.message) || e || '').trim();
    const code = (/^HTTP (\d{3})\b/.exec(t) || [])[1];
    let msg = null;
    try{
      const j = JSON.parse(t);
      if(j && typeof j.detail === 'string') msg = j.detail;
      else if(j && Array.isArray(j.detail)) return 'некорректный запрос';
    }catch(_){}
    if(msg === null){
      if(/^</.test(t)) return 'сервер не ответил вовремя — подождите минуту и повторите';   // страница-заглушка nginx (502/504)
      msg = t;
    }
    msg = msg.trim().slice(0, 200);
    if(!msg) return 'неизвестная ошибка';
    return /[А-Яа-яЁё]/.test(msg) ? msg : 'Сервер ответил ошибкой' + (code ? ' (HTTP ' + code + ')' : '') + '. Повторите позже';
  }

  // Размер: МБ с одним знаком, от 1024 МБ — ГБ с одним знаком («640,0 МБ», «18,6 ГБ»)
  function fmtSize(bytes){
    const b = Number(bytes);
    if(!isFinite(b) || b < 0) return '—';
    const mb = Math.round(b / 1048576 * 10) / 10;
    if(mb >= 1024) return (Math.round(b / 1073741824 * 10) / 10).toFixed(1).replace('.', ',') + ' ГБ';
    return mb.toFixed(1).replace('.', ',') + ' МБ';
  }

  // Дата по Москве «ГГГГ-ММ-ДД»; shift — сдвиг в сутках (-1 — вчера)
  function mskDate(shift){
    return new Date(Date.now() + 3 * 3600e3 + (shift || 0) * 86400e3).toISOString().slice(0, 10);
  }

  // «2026-11-03…» -> «03.11.2026»
  function dmy(iso){
    const s = String(iso || '');
    return s.length >= 10 ? s.slice(8, 10) + '.' + s.slice(5, 7) + '.' + s.slice(0, 4) : '';
  }

  // «18:42»
  function hm(iso){ return String(iso || '').slice(11, 16); }

  // «сегодня, 09:15» / «вчера, 18:42» / «02.11.2026, 18:42». isToday — «сегодня» по мнению сервера
  // (true / false), если он его знает: тогда с часами этого компьютера не спорим.
  function whenText(iso, isToday){
    const s = String(iso || '');
    if(s.length < 16) return '';
    const day = s.slice(0, 10);
    const known = typeof isToday === 'boolean';
    let d;
    if(known ? isToday : day === mskDate(0)) d = 'сегодня';
    else if(day === mskDate(-1)) d = 'вчера';
    else d = dmy(day);
    return d + ', ' + hm(s);
  }

  // Ответ с состоянием годится для рисования
  function sane(s){
    return !!s && typeof s === 'object' && !!s.password && !!s.copy && !!s.media;
  }

  // Состояние для рисования: только у суперадмина и только то, что получено под этим же входом
  function cur(){
    return S && SUser === ME.username && isSuper() ? S : null;
  }

  function setStatus(s){
    S = s;
    SUser = ME.username;
  }

  function focusedAction(box){
    const a = document.activeElement;
    return a && a.dataset && a.dataset.action && box.contains(a) ? a.dataset.action : null;
  }

  function restoreFocus(box, action){
    if(!action) return;
    const n = box.querySelector(`[data-action="${action}"]:not([disabled])`);
    if(n) n.focus({preventScroll: true});
  }

  // ---- напоминание на дашборде ---------------------------------------------------

  function bannerContent(s){
    const cp = s.copy || {}, md = s.media || {}, pw = s.password || {};
    const by = who => who ? ' (' + who + ')' : '';          // без esc: экранируем один раз, при вставке в разметку
    const dot = col => `<span class="dot" style="background:${col};flex:none;"></span>`;

    if(s.state === 'ok'){
      return `<div class="banner on" role="group" aria-label="Копия на флешку">
        ${dot('var(--accent)')}
        <div style="flex:1;min-width:200px;">
          <div class="oc-cap" style="color:var(--accent);">Копия на флешку</div>
          <div style="font-weight:600;">${esc('Сегодня скачана в ' + hm(cp.last_at) + by(cp.last_by))}</div>
          <div class="muted" style="font-size:12px;">Новые ролики тоже скачаны</div>
        </div>
      </div>`;
    }
    if(s.state !== 'warn') return '';

    const nNew = Number(md.new_count) || 0;
    const title = cp.today ? 'Копия сегодня скачана, а новые ролики — нет' : 'Сегодня копию ещё не скачивали';
    const bits = [cp.last_at ? 'Последняя — ' + whenText(cp.last_at, cp.today) + by(cp.last_by)
                             : 'Раньше копию не скачивали'];
    if(nNew > 0) bits.push('новых роликов с тех пор: ' + nNew + ', ' + fmtSize(md.new_bytes));

    let btn = '';
    if(!cp.today){
      btn = pw.set
        ? '<button type="button" class="btn primary" data-action="offsite-copy">⤓ Скачать копию</button>'
        : '<button type="button" class="btn primary" data-action="offsite-password">Задать пароль на копию</button>';
    }else if(nNew > 0){
      btn = `<button type="button" class="btn primary" data-action="offsite-media-new"${mediaBusy ? ' disabled' : ''}>⤓ Скачать новые ролики</button>`;
    }
    return `<div class="banner warn" role="group" aria-label="Копия на флешку">
      ${dot('#ffd34d')}
      <div style="flex:1;min-width:200px;">
        <div class="oc-cap" style="color:#ffd34d;">Копия на флешку</div>
        <div style="font-weight:600;">${esc(title)}</div>
        <div class="muted" style="font-size:12px;">${esc(bits.join(' · '))}</div>
      </div>
      ${btn}
    </div>`;
  }

  // Записать напоминание из последнего состояния; то же самое в том же контейнере не трогаем
  function paintBanner(){
    const box = $('offsite-banner');
    if(!box) return;
    const s = cur();
    const html = s ? bannerContent(s) : '';
    if(box === bannerBox && html === bannerHtml) return;
    const act = focusedAction(box);
    box.innerHTML = html;
    bannerBox = box;
    bannerHtml = html;
    restoreFocus(box, act);
  }

  async function loadOffsiteBanner(){
    if(!isSuper()){ S = null; return; }          // не суперадмин — запроса нет вовсе
    if(!$('offsite-banner')) return;
    paintBanner();                               // из запомненного — до ответа сервера, чтобы не мигало
    const my = ++bannerSeq;
    let s;
    try{
      s = await api(STATUS);
      if(!sane(s)) throw new Error('bad status');
    }catch(e){
      if(my !== bannerSeq) return;
      if(String((e && e.message) || '') === 'unauthorized') return;
      S = null;                                  // старый сервер или нет связи: без напоминания
      paintBanner();
      return;
    }
    if(my !== bannerSeq) return;
    setStatus(s);
    paintBanner();
  }

  // ---- блок в Настройках -----------------------------------------------------------

  function blockContent(s){
    const pw = s.password || {}, cp = s.copy || {}, md = s.media || {};
    const mounted = s.project_mounted !== false;
    const by = who => who ? ' · ' + esc(who) : '';
    const nNew = Number(md.new_count) || 0, nAll = Number(md.all_count) || 0;

    let copyBtn;
    if(!pw.set){
      copyBtn = '<button type="button" class="btn primary" data-action="offsite-password">Задать пароль на копию</button>';
    }else{
      const size = cp.last_bytes ? ' · ≈ ' + fmtSize(cp.last_bytes) : '';
      copyBtn = `<button type="button" class="btn primary" data-action="offsite-copy"${mounted ? '' : ` disabled title="${esc(MSG_NOT_MOUNTED)}"`}>⤓ Скачать копию${size}</button>`;
    }

    let pwVal;
    if(pw.set){
      pwVal = 'задан' + (pw.set_at ? ' ' + esc(dmy(pw.set_at)) : '') + (pw.set_by ? ' (' + esc(pw.set_by) + ')' : '')
        + ' · <button type="button" class="btn" style="padding:4px 9px;font-size:12px;" data-action="offsite-password">Сменить</button>';
    }else{
      pwVal = pw.needs_reset ? 'нужно задать заново' : 'не задан';
    }

    const newBtn = `<button type="button" class="btn${nNew > 0 ? ' primary' : ''}" data-action="offsite-media-new"${nNew > 0 && !mediaBusy ? '' : ' disabled'}>⤓ Скачать новые · ${nNew} шт${nNew > 0 ? ', ' + esc(fmtSize(md.new_bytes)) : ''}</button>`;
    const allBtn = `<button type="button" class="btn" data-action="offsite-media-all"${nAll > 0 && !mediaBusy ? '' : ' disabled'}>⤓ Скачать все · ${nAll} шт${nAll > 0 ? ', ' + esc(fmtSize(md.all_bytes)) : ''}</button>`;
    const since = md.last_at ? whenText(md.last_at) : 'ролики ещё не скачивали — новыми считаются все';

    return `<div class="muted" style="font-size:12px;margin-bottom:12px;max-width:720px;">Сохраните копию на свой ПК или съёмный диск. Если сервер сломается, по ней систему поднимут на новой машине — инструкция лежит внутри архива. Копия заперта паролем: без него её не открыть.</div>
      <div class="cell" style="margin-bottom:10px;">
        <div class="oc-head"><b>Ежедневная копия</b>${copyBtn}</div>
        ${mounted ? '' : `<div class="oc-warn" role="note">⚠ ${esc(MSG_NOT_MOUNTED)}</div>`}
        <div class="oc-kv">
          <div class="k">Что внутри</div><div class="v">база, пароли сервера (.env), документы рекламодателей, файлы OTA, программа сервера, инструкция</div>
          <div class="k">Последнее скачивание</div><div class="v">${cp.last_at ? esc(whenText(cp.last_at, cp.today)) + by(cp.last_by) : 'ещё не скачивали'}</div>
          <div class="k">Пароль на копию</div><div class="v">${pwVal}</div>
        </div>
      </div>
      <div class="cell">
        <div class="oc-head"><b>Ролики</b>${newBtn}${allBtn}</div>
        <div class="oc-kv">
          <div class="k">Новые — это</div><div class="v">добавленные после прошлого скачивания (${esc(since)})</div>
          <div class="k">При восстановлении</div><div class="v">все архивы роликов распаковываются в одну папку</div>
        </div>
      </div>`;
  }

  function paintBlock(){
    const el = $('offsite-block');
    if(!el) return;
    const s = cur();
    let html;
    if(s) html = blockContent(s);
    else if(blockState === 'fail') html = '<div style="font-size:12px;">Не удалось загрузить состояние копии</div>';
    else return;                                 // ещё грузится: остаётся «Загрузка…» из разметки Настроек
    if(el === blockBox && html === blockHtml) return;
    const act = focusedAction(el);
    el.classList.toggle('muted', !s);
    el.innerHTML = html;
    blockBox = el;
    blockHtml = html;
    restoreFocus(el, act);
  }

  async function loadOffsiteBlock(){
    if(!isSuper() || !$('offsite-block')) return;
    blockState = cur() ? 'ok' : 'loading';
    paintBlock();                                // из запомненного — без «Загрузка…» на каждый заход
    const my = ++blockSeq;
    let s;
    try{
      s = await api(STATUS);
      if(!sane(s)) throw new Error('bad status');
    }catch(e){
      if(my !== blockSeq) return;
      if(String((e && e.message) || '') === 'unauthorized') return;
      S = null;
      blockState = 'fail';
      paintBlock();
      return;
    }
    if(my !== blockSeq) return;
    setStatus(s);
    blockState = 'ok';
    paintBlock();
  }

  // Перечитать то, что сейчас на экране (одним запросом: на дашборде есть напоминание, в Настройках — блок)
  function refreshView(){
    if($('offsite-block')) loadOffsiteBlock();
    else if($('offsite-banner')) loadOffsiteBanner();
  }

  // Состояние поменялось из-за нашего действия: перерисовать из запомненного (кнопки «занято»)
  function repaint(){
    paintBanner();
    paintBlock();
  }

  // ---- слежение за скачиванием ------------------------------------------------------
  // Сервер отмечает «скачано», только когда отдан последний байт, а nginx отдаёт не быстрее 20 МБ/с:
  // архив роликов идёт минуты, а то и десятки минут. Поэтому после начала скачивания состояние
  // перечитывается: через 3 с (малая копия) и дальше каждые 15 с. Скачивание «дошло», когда last_at
  // (копии или роликов) стал другим, чем был в момент клика. Цикл один на всех: новое скачивание
  // добавляет своё слежение и взводит таймер заново. Он кончается, когда дождались всех, когда со
  // страницы ушли оба контейнера (напоминание и блок), когда сменился вход и через час. Кнопки роликов
  // заняты (mediaBusy), пока слежение за роликами не кончилось: второй клик не начнёт второе скачивание.

  function hasWatch(){ return !!(watch.copy || watch.media); }

  function armPoll(ms){
    clearTimeout(pollTimer);
    pollTimer = setTimeout(pollTick, ms);
  }

  // kind — 'copy' | 'media'; from — last_at (копии или роликов) на момент клика
  function watchDownload(kind, from){
    watch[kind] = {from: from || null, until: Date.now() + POLL_CAP_MS};
    watchUser = ME.username;
    armPoll(POLL_FIRST_MS);
  }

  function stopWatch(){
    watch = {};
    clearTimeout(pollTimer);
    pollTimer = 0;
    mediaBusy = false;
    repaint();
  }

  async function pollTick(){
    pollTimer = 0;
    if(!hasWatch()) return;
    if(!isSuper() || ME.username !== watchUser || (!$('offsite-banner') && !$('offsite-block'))) return stopWatch();
    let s = null;
    try{
      s = await api(STATUS);
      if(!sane(s)) s = null;
    }catch(e){
      if(String((e && e.message) || '') === 'unauthorized') return stopWatch();
      // связь мигнула — ждём следующего круга, слежение не снимаем
    }
    if(!hasWatch()) return;                      // за время запроса слежение сняли
    if(s){
      bannerSeq++; blockSeq++;                   // запоздавшие ответы обычных загрузок не затрут свежее
      setStatus(s);
      blockState = 'ok';
      if(watch.copy && (s.copy || {}).last_at !== watch.copy.from) delete watch.copy;
      if(watch.media && (s.media || {}).last_at !== watch.media.from){ delete watch.media; mediaBusy = false; }
    }
    const now = Date.now();
    for(const k of ['copy', 'media']){           // час прошёл, а отметки нет: скачивание оборвали — больше не ждём
      if(watch[k] && now >= watch[k].until){
        delete watch[k];
        if(k === 'media') mediaBusy = false;
      }
    }
    repaint();
    if(hasWatch() && !pollTimer) armPoll(POLL_MS);
  }

  // ---- скачивание -----------------------------------------------------------------

  // Открыть одноразовую ссылку скачиванием. false — сервер прислал не то, что мы ждём.
  function startDownload(url, filename){
    if(typeof url !== 'string' || !url.startsWith(DL_PREFIX)) return false;
    const a = document.createElement('a');
    a.href = url;
    a.download = typeof filename === 'string' ? filename : '';
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    a.remove();
    return true;
  }

  async function downloadMedia(scope){
    if(mediaBusy) return;
    const s = cur();
    if(!s){ toast(MSG_NO_STATE); return; }
    const md = s.media || {};
    if(scope === 'all'){
      const n = Number(md.all_count) || 0;
      if(!n){ toast('В медиатеке нет роликов'); return; }
      if(!confirm(`Скачать все ролики: ${n} шт, ${fmtSize(md.all_bytes)}?\n\nАрхив большой, скачивание может занять много времени.`)) return;
    }
    const from = md.last_at || null;             // метка прошлого скачивания: когда она сменится, файл дошёл
    mediaBusy = true;
    repaint();
    let held = false;                            // кнопки остаются занятыми, пока идёт слежение за скачиванием
    try{
      const r = await api('/offsite/media', {method: 'POST', body: JSON.stringify({scope})});
      if(!startDownload(r && r.url, r && r.filename)){ toast('Сервер не прислал ссылку на скачивание — повторите'); return; }
      toast(MSG_STARTED);
      const miss = Number(r.missing) || 0;
      if(miss > 0) setTimeout(() => toast('В архиве не хватает файлов на сервере: ' + miss), 2400);   // после первой плашки
      held = true;
      watchDownload('media', from);
    }catch(e){
      if(String((e && e.message) || '') === 'unauthorized') return;
      toast(errText(e));
      refreshView();                             // число «новых» могло устареть (409 «Новых роликов нет»)
    }finally{
      if(!held){ mediaBusy = false; repaint(); }
    }
  }

  // ---- окна ---------------------------------------------------------------------------

  function showErr(text){
    const el = $('oc-err');
    if(!el) return;
    el.textContent = text || '';
    el.hidden = !text;
  }

  function setBusy(st, on){
    st.busy = on;
    st.modal.el.querySelector('.modal-body').inert = on;     // форму на время запроса замораживаем
    const submit = $('oc-submit'), cancel = $('oc-cancel'), wait = $('oc-wait');
    if(submit){
      submit.disabled = on;
      submit.textContent = on ? st.busyText : st.submitText;
    }
    if(cancel) cancel.disabled = on;
    if(wait) wait.hidden = !on;
    if(on) st.modal.el.focus({preventScroll: true});         // отключённая кнопка фокус не держит
  }

  function openWindow(kind, o){
    const st = W = {kind, busy: false, closed: false, modal: null, submitText: o.submitText, busyText: o.busyText};
    const modal = openModal({
      title: o.title,
      body: o.body,
      footer: `<button type="button" class="btn" id="oc-cancel" data-modal-close>Отмена</button>
        <button type="button" class="btn primary" id="oc-submit" data-action="${o.action}">${o.submitText}</button>`,
      width: o.width,
      className: 'modal-oc',
      onClose(){
        st.closed = true;
        if(W === st) W = null;
      },
    });
    st.modal = modal;

    // Пока идёт запрос, окно не закрывается ни ✕, ни кликом по фону (ответ ушёл бы «в никуда»).
    // Клики — в capture-фазе на подложке, раньше обработчика modal.js; Esc гасит обработчик внизу файла.
    const back = modal.el.parentElement;
    back.addEventListener('mousedown', e => { if(st.busy && e.target === back) e.preventDefault(); }, true);
    back.addEventListener('click', e => {
      if(!st.busy) return;
      const x = e.target.closest && e.target.closest('[data-modal-close]');
      if(e.target === back || (x && modal.el.contains(x))){
        e.stopImmediatePropagation();
        e.preventDefault();
      }
    }, true);
    return st;
  }

  function field(id, label, auto, note, autofocus){
    return `<div class="fld"><label for="${id}">${label}</label>
      <input class="inp" id="${id}" type="password" autocomplete="${auto}"${autofocus ? ' autofocus' : ''}>
      ${note ? `<div class="oc-sub">${note}</div>` : ''}</div>`;
  }

  // Окно «Пароль на копию»: задать или сменить
  function openPasswordWindow(){
    if(W) return;
    const s = cur();
    if(!s){ toast(MSG_NO_STATE); return; }
    const needCurrent = !!(s.password && s.password.set);     // смена требует текущий пароль на копию
    const st = openWindow('password', {
      title: 'Пароль на копию',
      width: 460,
      action: 'offsite-password-save',
      submitText: 'Сохранить пароль',
      busyText: 'Сохраняю…',
      body: `<div class="oc-form">
        ${needCurrent ? field('oc-pw-current', 'Текущий пароль на копию', 'off', '', true) : ''}
        ${field('oc-pw-new', 'Новый пароль', 'off', 'Не короче 10 символов, только латинские буквы, цифры и знаки (без пробелов)', !needCurrent)}
        ${field('oc-pw-new2', 'Ещё раз', 'off')}
        ${field('oc-pw-login', 'Ваш пароль для входа в панель', 'current-password')}
        <div class="oc-hint" role="note">${esc(MSG_PW_WARN)}</div>
        <div class="oc-err" id="oc-err" role="alert" hidden></div>
      </div>`,
    });
    st.needCurrent = needCurrent;
  }

  async function submitPassword(){
    const st = W;
    if(!st || st.kind !== 'password' || st.busy) return;
    const v = id => { const e = $(id); return e ? e.value : ''; };
    const stop = (id, text) => { showErr(text); const e = $(id); if(e) e.focus({preventScroll: true}); };
    const body = {new_password: v('oc-pw-new'), new_password2: v('oc-pw-new2'), login_password: v('oc-pw-login')};
    if(st.needCurrent){
      body.current_password = v('oc-pw-current');
      if(!body.current_password) return stop('oc-pw-current', 'Введите текущий пароль на копию');
    }
    if(!body.new_password) return stop('oc-pw-new', 'Введите новый пароль');
    if(body.new_password !== body.new_password2) return stop('oc-pw-new2', 'Пароли не совпадают');
    if(!body.login_password) return stop('oc-pw-login', 'Введите пароль для входа в панель');

    setBusy(st, true);                           // сразу, до await: второй клик ничего не сделает
    showErr('');
    try{
      await api('/offsite/password', {method: 'POST', body: JSON.stringify(body)});
    }catch(e){
      if(String((e && e.message) || '') === 'unauthorized'){ if(!st.closed) st.modal.close(); return; }
      if(st.closed) return;
      setBusy(st, false);
      const msg = errText(e);                    // 400 / 403 приходят русским текстом
      showErr(msg);
      // курсор — в то поле, где ошибка: текущий пароль на копию, пароль входа или новый пароль
      const bad = $(st.needCurrent && /текущ/i.test(msg) ? 'oc-pw-current' : /входа/i.test(msg) ? 'oc-pw-login' : 'oc-pw-new');
      if(bad){ bad.focus({preventScroll: true}); bad.select(); }
      return;
    }
    if(!st.closed) st.modal.close();
    toast('Пароль на копию сохранён');
    refreshView();
  }

  // Окно «Скачать копию на флешку» (макет 3)
  function openCopyWindow(){
    if(W) return;
    const s = cur();
    if(!s){ toast(MSG_NO_STATE); return; }
    if(s.project_mounted === false){ toast(MSG_NOT_MOUNTED); return; }
    if(!(s.password && s.password.set)){ openPasswordWindow(); return; }   // сначала пароль на копию
    const iso = new Date(Date.now() + 3 * 3600e3).toISOString();           // по Москве: так же называет файл сервер
    const name = 'ds_copy_' + iso.slice(0, 10) + '_' + iso.slice(11, 13) + iso.slice(14, 16) + '.tar';
    openWindow('copy', {
      title: 'Скачать копию на флешку',
      width: 470,
      action: 'offsite-copy-go',
      submitText: '⤓ Скачать',
      busyText: 'Собираю копию…',
      body: `<div class="oc-form">
        <div class="oc-lead">В копии — все пароли сервера и вся база. Подтвердите, что это вы.</div>
        ${field('oc-login', 'Ваш пароль для входа в панель', 'current-password', '', true)}
        <div class="oc-hint">Файл <b>${esc(name)}</b> сохранится туда, куда браузер сохраняет загрузки. Чтобы выбрать флешку, включите в браузере «Всегда спрашивать, куда сохранять файлы».</div>
        <div class="oc-wait" id="oc-wait" role="status" hidden>Это может занять несколько минут — не закрывайте вкладку.</div>
        <div class="oc-err" id="oc-err" role="alert" hidden></div>
      </div>`,
    });
  }

  async function submitCopy(){
    const st = W;
    if(!st || st.kind !== 'copy' || st.busy) return;
    const inp = $('oc-login');
    const pass = inp ? inp.value : '';
    if(!pass){
      showErr('Введите пароль для входа в панель');
      if(inp) inp.focus({preventScroll: true});
      return;
    }
    const s0 = cur();
    const from = (s0 && s0.copy && s0.copy.last_at) || null;   // метка прошлой копии: когда сменится, файл дошёл
    setBusy(st, true);                           // сразу, до await: второй клик ничего не сделает
    showErr('');
    let r;
    try{
      r = await api('/offsite/copy', {method: 'POST', body: JSON.stringify({login_password: pass})});
    }catch(e){
      if(String((e && e.message) || '') === 'unauthorized'){ if(!st.closed) st.modal.close(); return; }
      if(st.closed) return;
      setBusy(st, false);
      showErr(errText(e));                       // неверный пароль, «копия уже собирается», «сервер не обновлён»…
      if(inp){ inp.focus({preventScroll: true}); inp.select(); }
      return;
    }
    if(!startDownload(r && r.url, r && r.filename)){
      if(st.closed) return;
      setBusy(st, false);
      showErr('Сервер не прислал ссылку на скачивание — повторите');
      return;
    }
    if(!st.closed) st.modal.close();
    toast(MSG_STARTED);
    watchDownload('copy', from);
  }

  // ---- действия -------------------------------------------------------------------------

  function initOffsiteActions(){
    if(window.__offsiteActionsInitialized) return;
    window.__offsiteActionsInitialized = true;

    document.addEventListener('click', e => {
      const el = e.target.closest && e.target.closest('[data-action]');
      if(!el) return;
      const action = el.dataset.action;
      if(!action || !action.startsWith('offsite-')) return;
      if(!isSuper()) return;
      switch(action){
        case 'offsite-copy':           return openCopyWindow();
        case 'offsite-password':       return openPasswordWindow();
        case 'offsite-media-new':      return downloadMedia('new');
        case 'offsite-media-all':      return downloadMedia('all');
        case 'offsite-copy-go':        return submitCopy();
        case 'offsite-password-save':  return submitPassword();
      }
    });

    // Enter в поле окна — то же, что кнопка
    document.addEventListener('keydown', e => {
      if(e.key !== 'Enter' || e.isComposing || !W || W.busy) return;
      const t = e.target;
      if(!t || t.tagName !== 'INPUT' || !W.modal.el.contains(t)) return;
      e.preventDefault();
      if(W.kind === 'copy') submitCopy(); else submitPassword();
    });

    // Esc во время запроса окно не закрывает (modal.js смотрит на defaultPrevented)
    document.addEventListener('keydown', e => {
      if(e.key === 'Escape' && W && W.busy) e.preventDefault();
    }, true);
  }

  window.loadOffsiteBanner = loadOffsiteBanner;
  window.loadOffsiteBlock = loadOffsiteBlock;
  window.Signage = window.Signage || {};
  window.Signage.loadOffsiteBanner = loadOffsiteBanner;
  window.Signage.loadOffsiteBlock = loadOffsiteBlock;

  initOffsiteActions();
})();
