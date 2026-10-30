/**
 * ui_test.js — браузерный прогон админ-панели (E2E).
 *
 * Зачем. Главный вывод пилотного развёртывания: почти все дефекты панели
 * (405 при создании экрана, нерабочее копирование токена, «мёртвые» кнопки
 * удаления/перезапуска) нашлись только живым кликом. smoke_test.sh проверяет
 * HTTP-эндпоинты и до интерфейса не доходит: кнопка может звать неверный
 * метод или падать в JS, а API при этом зелёный.
 *
 * Что делает:
 *   1. Входит в панель как администратор.
 *   2. Проходит ВСЕ разделы левого меню, проверяя, что вью отрисовалось.
 *   3. Жмёт все «безопасные» кнопки раздела (открыть форму, вкладку, фильтр,
 *      отчёт) — список действий белый, разрушающие не трогаются НИКОГДА.
 *   4. Прогоняет циклы с уборкой за собой: экран (создать → увидеть →
 *      удалить) и ПЛЕЙЛИСТ целиком — окно «Новый плейлист» (поиск, галочка,
 *      повторы ×N, предупреждение «подряд»), шаг «Куда и когда» (только свой
 *      тестовый экран, завтра, 10–11 ч, пересечения), строка «запланирован» в
 *      карточке, режим «По датам» в Расписании, отмена показа и удаление
 *      плейлиста.
 *      Плюс просмотр ролика в Медиатеке.
 *   5. «Эфир сети»: правило «в сети» (в «Экранах» и на дашборде экран без
 *      сигнала — «ни разу не выходил на связь», после heartbeat — в сети),
 *      таблица страницы со своим тестовым экраном, поиск, панель «О плейлисте»
 *      (ролики, «Где и когда идёт», просмотр ролика), «✎ Изменить плейлист» и
 *      «📤 Отправить на экраны» поверх страницы (закрываются Esc без сохранения),
 *      полоса общего эфира и окно включения (отмена ничего не отправляет).
 *      Само ВКЛЮЧЕНИЕ эфира идёт на все экраны сети и выполняется только при
 *      E2E_ALLOW_BROADCAST=1 (на стенде): до следующего часа → жёлтая полоса →
 *      «Сменить плейлист» (окно) → «Выключить»; прежнее состояние эфира
 *      возвращается в конце, даже если проверка упала.
 *   6. Копит ошибки консоли, необработанные исключения и ответы HTTP ≥400.
 *
 * Любая ошибка консоли/JS и любой ответ ≥400 — это провал прогона: именно
 * так выглядели дефекты, которые ловили руками.
 *
 * Тестовые объекты. Для плейлиста нужны экран и одобренный ролик, а на пустом
 * стенде их нет: прогон сам заводит экран «E2E_стенд_…» и картинку-ролик
 * «E2E_ролик_…» (служебная категория — одобрена без модерации) и в конце
 * убирает ВСЁ, что создал, — и когда проверка упала. Плейлист с показом,
 * созданный по ходу, удаляется страховочной уборкой по названию. Показ идёт
 * только на собственный экран прогона «E2E_стенд_…», а не на «Все экраны
 * сети»: даже при жёстком обрыве прогона настоящие экраны его не получат.
 * Единственное исключение — общий эфир при E2E_ALLOW_BROADCAST=1: он по своей
 * природе идёт на ВСЕ экраны сети (только на стенде), и при жёстком обрыве
 * прогона его нужно выключить вручную.
 *
 * Запуск — через ui_test.sh (поднимает контейнер с Chromium).
 * Переменные: DS_URL (по умолч. http://nginx), ADMIN_USER, ADMIN_PASS,
 * E2E_ALLOW_BROADCAST=1 — шаг с включением общего эфира (он идёт на ВСЕ экраны,
 * только на стенде; без переменной шаг пропускается с пометкой).
 */
const puppeteer = require('puppeteer');

const BASE = process.env.DS_URL || 'http://nginx';
const USER = process.env.ADMIN_USER || 'admin';
const PASS = process.env.ADMIN_PASS || 'admin123';
const STAMP = new Date().toISOString().slice(11, 19).replace(/:/g, '');
const SCREEN_NAME = `E2E_экран_${STAMP}`;
const PLAYLIST_NAME = `E2E_плейлист_${STAMP}`;
const CLIP_TITLE = `E2E_ролик_${STAMP}`;
const CLIP_QUERY = `ролик_${STAMP}`;        // часть названия: проверяем и поиск по подстроке
const FIXTURE_SCREEN = `E2E_стенд_${STAMP}`;
const DUP_TITLE = `E2E_дубль_${STAMP}`;
const NL_PLAYLIST = `E2E_эфир_${STAMP}`;      // плейлист для страницы «Эфир сети» (свой, отдельно от цикла «плейлист»)
const BC_ALLOWED = process.env.E2E_ALLOW_BROADCAST === '1';

// Разделы левого меню: [data-nav, заголовок в шапке].
const SECTIONS = [
  ['dash', 'Дашборд'], ['broadcast', 'Эфир сети'], ['ticker', 'Бегущая строка'],
  ['screens', 'Экраны'], ['media', 'Медиатека'], ['playlists', 'Плейлисты'],
  ['schedule', 'Расписание'], ['campaigns', 'Кампании'], ['moderation', 'Модерация'],
  ['reports', 'Отчёты'], ['advertisers', 'Рекламодатели'], ['billing', 'Биллинг'],
  ['ota', 'Обновление агентов'], ['settings', 'Настройки'],
];

// БЕЛЫЙ список кнопок для слепого прогона: только то, что открывает форму,
// вкладку, модалку или читает данные. Всё, чего здесь нет (удаление, отправка
// форм, эфир, OTA, команды экрану, выгрузки файлов), не нажимается — список
// намеренно перечислен поимённо, а не шаблоном, чтобы новая разрушающая
// кнопка не попала в прогон случайно.
const SAFE_ACTIONS = new Set([
  'switch-screens-tab', 'open-screen-add-form', 'cancel-create-screen',
  'open-group-add-form', 'cancel-create-group', 'open-screen-settings',
  'close-screen-settings', 'open-sync-start-form', 'close-sync-modal',
  'open-add-to-group-form', 'close-group-modal', 'copy-screen-token',
  'screen-offline-diagnostics', 'screen-refresh-diag',
  'media-open-upload', 'media-open-adv', 'media-open-common', 'media-open-fillers',
  'media-back', 'media-select-all', 'media-common-folder',
  'playlists-open', 'playlists-back',
  'mprev-open',
  'schedule-mode-week', 'schedule-mode-month', 'schedule-change-month',
  'schedule-mode-dates', 'schedule-week-next',
  'schedule-toggle-list', 'schedule-target-network', 'schedule-target-group',
  'schedule-target-screen', 'schedule-select-target', 'schedule-simulate',
  'campaigns-open-create', 'campaigns-cancel-create', 'campaigns-toggle-daily',
  'moderation-preview',
  'reports-open-playlog', 'reports-load-playlog', 'reports-open-builder',
  'reports-back-to-list', 'reports-toggle-section', 'reports-exp-all-true',
  'reports-exp-all-false', 'reports-dash-apply', 'reports-dash-reset',
  'reports-open-export', 'reports-close-export', 'reports-builder-run',
  'reports-apply-saved',
  'billing-preview', 'billing-details',
  'ota-refresh',
  'settings-run-selfcheck', 'settings-open-my-password-form',
  'settings-cancel-my-password', 'settings-edit-user',
  'settings-close-edit-user-modal',
  'ticker-target-all', 'ticker-target-one', 'ticker-pick-color', 'ticker-toggle-screen',
  'dashboard-open-broadcast',
  'nl-open-playlist', 'nl-close-drawer',
  'adv-open', 'adv-back', 'adv-tab', 'adv-apply-period',
]);

// Шум, который не является дефектом панели.
const IGNORE_URL = [/\/favicon\.ico/];
const IGNORE_TEXT = [/Failed to load resource: the server responded with a status of 401/];

let PASS_N = 0, FAIL_N = 0;
const problems = [];      // накопленные ошибки консоли/HTTP
let step = 'старт';       // текущий шаг — для привязки ошибки к месту
let dialogMode = 'dismiss';   // как отвечать на confirm/prompt
let promptAnswer = '';

const ok = (m) => { PASS_N++; console.log('  ✓ ' + m); };
const bad = (m) => { FAIL_N++; console.log('  ✗ ' + m); };
const head = (m) => console.log('\n▶ ' + m);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// Ожидаемые «ошибки» на время одного шага (например, 409 «такой ролик уже есть» —
// сервер так и должен ответить на повторную загрузку). Список очищает сам шаг.
let expected = [];

function record(kind, text) {
  if (IGNORE_TEXT.some(re => re.test(text))) return;
  if (expected.some(re => re.test(`${kind}: ${text}`))) return;
  problems.push(`[${step}] ${kind}: ${text}`);
}

async function waitViewReady(page) {
  // Вью рисуются асинхронно: ждём, пока уйдёт «Загрузка…» и появится контент.
  await page.waitForFunction(() => {
    const v = document.getElementById('view');
    return v && v.innerText.trim() !== '' && !/^Загрузка…$/.test(v.innerText.trim());
  }, { timeout: 15000 });
  await sleep(250);
}

async function gotoSection(page, nav) {
  await page.click(`.nav[data-nav="${nav}"]`);
  await waitViewReady(page);
}

/** Видимые элементы с data-action из белого списка (в порядке появления). */
async function safeButtons(page) {
  return page.evaluate((safe) => {
    const out = [];
    document.querySelectorAll('#view [data-action], #topright [data-action]').forEach((el, i) => {
      const a = el.dataset.action;
      if (!safe.includes(a)) return;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return;      // скрытые не жмём
      out.push({ action: a, text: (el.textContent || '').trim().slice(0, 40) });
    });
    return out;
  }, [...SAFE_ACTIONS]);
}

async function clickNth(page, action, n) {
  const handles = await page.$$(`#view [data-action="${action}"], #topright [data-action="${action}"]`);
  if (!handles[n]) return false;
  await handles[n].click();
  return true;
}

/** Закрыть открытые окна клавишей Esc (как это сделал бы человек). Безопасные
 *  кнопки вроде «Просмотр» открывают модальное окно поверх страницы — без
 *  закрытия следующий клик по меню попал бы в затемнённый фон. */
async function closeModals(page) {
  for (let i = 0; i < 4; i++) {
    if (!await page.$('.modal-backdrop')) return true;
    await page.keyboard.press('Escape');
    await sleep(250);
  }
  return !await page.$('.modal-backdrop');
}

/** Ждать условия в странице; true/false вместо исключения по таймауту. */
const waitUntil = (page, fn, arg, timeout = 10000) =>
  page.waitForFunction(fn, { timeout }, arg).then(() => true, () => false);

/** Запрос к API от имени вошедшего администратора (токен берётся из страницы).
 *  opts.json — JSON-тело; opts.file {b64, type, name} — загрузка файла. */
async function apiCall(page, method, path, opts = {}) {
  return page.evaluate(async (method, path, opts) => {
    const headers = { 'Authorization': 'Bearer ' + localStorage.getItem('ds_token') };
    let body;
    if (opts.file) {
      body = new FormData();
      const bin = Uint8Array.from(atob(opts.file.b64), c => c.charCodeAt(0));
      body.append('file', new Blob([bin], { type: opts.file.type }), opts.file.name);
    } else if (opts.json) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(opts.json);
    }
    const r = await fetch('/api' + path, { method, headers, body });
    let data = null;
    try { data = await r.json(); } catch (_) { /* пустое тело */ }
    return { status: r.status, data };
  }, method, path, opts);
}

/** Минимальный валидный PNG 64×36 (цвет и текстовая метка зависят от stamp —
 *  у каждого прогона свой md5, окно «такой ролик уже есть» не мешает). */
function makePng(stamp) {
  const zlib = require('zlib');
  const table = new Uint32Array(256).map((_, n) => {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  const crc32 = (buf) => {
    let c = 0xFFFFFFFF;
    for (const b of buf) c = table[(c ^ b) & 0xFF] ^ (c >>> 8);
    return (c ^ 0xFFFFFFFF) >>> 0;
  };
  const chunk = (type, data) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
    const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(body));
    return Buffer.concat([len, body, crc]);
  };
  const W = 64, H = 36;
  const n = parseInt(stamp, 10) || 0;
  const px = Buffer.from([40 + n % 180, 60 + (n >> 3) % 160, 90 + (n >> 6) % 140]);
  const row = Buffer.concat([Buffer.from([0]), ...Array(W).fill(px)]);      // фильтр 0 + RGB
  const raw = Buffer.concat(Array(H).fill(row));
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(W, 0); ihdr.writeUInt32BE(H, 4);
  ihdr[8] = 8; ihdr[9] = 2;                                                 // 8 бит, RGB
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]),
    chunk('IHDR', ihdr),
    chunk('tEXt', Buffer.from('Comment\0e2e-' + stamp, 'latin1')),
    chunk('IDAT', zlib.deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

// «Сегодня» по Москве (так считает и сама панель) и сдвиг даты на n суток.
const MSK_FMT = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Europe/Moscow', year: 'numeric', month: '2-digit', day: '2-digit' });
function mskToday() {
  const parts = MSK_FMT.formatToParts(new Date());
  const g = t => parts.find(p => p.type === t).value;
  return `${g('year')}-${g('month')}-${g('day')}`;
}
function addDays(iso, n) {
  const d = new Date(iso + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

const hh = n => String(n).padStart(2, '0');

/** Сигнал тестового экрана «как у агента»: X-Token, статус online, играет ролик file.
 *  Возвращает HTTP-код. Экран после него «в сети» (последний сигнал не старше 5 минут). */
async function heartbeat(page, id, token, file) {
  return page.evaluate(async (id, token, file) => {
    const r = await fetch('/api/heartbeat/' + id, {
      method: 'POST',
      headers: { 'X-Token': token, 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'online', playing_file: file || null }),
    });
    return r.status;
  }, id, token, file);
}

/** «Сейчас» глазами сервера (наивное МСК из GET /network/live): дата, час, минута, секунда. */
async function serverNow(page) {
  const r = await apiCall(page, 'GET', '/network/live');
  const g = String((r.data || {}).generated_at || '');        // «2026-10-29T13:56:05»
  return { iso: g.slice(0, 19), date: g.slice(0, 10), hour: parseInt(g.slice(11, 13), 10),
           minute: parseInt(g.slice(14, 16), 10), second: parseInt(g.slice(17, 19), 10) };
}

/** Час «до» для POST /broadcast/on по сроку эфира из GET /broadcast: до 24:00 = 24, null — «до отмены». */
function untilHourOf(untilAt) {
  if (!untilAt) return null;
  const h = parseInt(untilAt.slice(11, 13), 10), m = parseInt(untilAt.slice(14, 16), 10),
        s = parseInt(untilAt.slice(17, 19), 10);
  return (h === 0 && m === 0 && s === 0) ? 24 : h + ((m || s) ? 1 : 0);
}

/** Вернуть общий эфир в состояние до прогона (если шаг его трогал).
 *  Лишних записей в журнале не плодит: состояние уже прежнее — ничего не шлёт.
 *  Флаг dirty сбрасывается только когда состояние ПОДТВЕРЖДЕНО (уже прежнее или запрос
 *  вернул 200): при исключении и при ответе не 200 он остаётся, и страховочная уборка в конце
 *  прогона повторит попытку.
 *  Возвращает эфир через POST /broadcast/on, поэтому в журнале появится новая запись
 *  «Общий эфир включён» (с новыми enabled_by / enabled_at). Если срок прежнего эфира
 *  вышел, пока шёл прогон, эфир кончился сам — правильное «прежнее» состояние тогда
 *  «выключен», и это не провал. */
async function restoreBroadcast(page, fx) {
  if (!fx.bc || !fx.bc.dirty) return;
  const prev = fx.bc.prev || {};
  const curR = await apiCall(page, 'GET', '/broadcast');
  if (curR.status !== 200) throw new Error(`GET /broadcast вернул HTTP ${curR.status}`);
  const cur = curR.data || {};
  // Прежний эфир «до ЧЧ:00» мог кончиться по сроку за время прогона: тогда он, как и положено, выключен.
  let wantOn = !!prev.is_on;
  let offText = 'общий эфир выключен, как и было';
  if (wantOn && prev.until_at && prev.until_at.slice(0, 19) <= (await serverNow(page)).iso) {
    wantOn = false;
    offText = 'прежний общий эфир кончился по сроку, пока шёл прогон, — теперь он выключен';
  }
  if (!wantOn) {
    if (!cur.is_on) { fx.bc.dirty = false; ok(offText); return; }
    const r = await apiCall(page, 'POST', '/broadcast/off');
    if (r.status === 200) { fx.bc.dirty = false; ok(offText); }
    else bad(`не удалось выключить общий эфир (HTTP ${r.status})`);
    return;
  }
  if (cur.is_on && cur.playlist_id === prev.playlist_id && cur.until_at === prev.until_at) {
    fx.bc.dirty = false; ok('прежний общий эфир на месте'); return;
  }
  const r = await apiCall(page, 'POST', '/broadcast/on',
    { json: { playlist_id: prev.playlist_id, until_hour: untilHourOf(prev.until_at) } });
  if (r.status === 200) { fx.bc.dirty = false; ok(`прежний общий эфир возвращён (плейлист #${prev.playlist_id})`); }
  else bad(`не удалось вернуть прежний общий эфир (HTTP ${r.status})`);
}

(async () => {
  console.log('════════════════════════════════════════════');
  console.log(' UI/E2E ТЕСТ ПАНЕЛИ — ' + new Date().toLocaleString('ru-RU'));
  console.log(' Адрес: ' + BASE);
  console.log('════════════════════════════════════════════');

  const browser = await puppeteer.launch({
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--lang=ru-RU'],
    defaultViewport: { width: 1440, height: 900 },
  });
  const page = await browser.newPage();

  page.on('console', m => { if (m.type() === 'error') record('console', m.text()); });
  page.on('pageerror', e => record('JS-исключение', e.message));
  page.on('requestfailed', r => {
    if (IGNORE_URL.some(re => re.test(r.url()))) return;
    record('запрос не выполнен', `${r.url()} — ${r.failure()?.errorText}`);
  });
  page.on('response', r => {
    if (r.status() < 400) return;
    if (IGNORE_URL.some(re => re.test(r.url()))) return;
    record('HTTP ' + r.status(), r.url());
  });
  page.on('dialog', async d => {
    if (dialogMode === 'accept') await d.accept(d.type() === 'prompt' ? promptAnswer : undefined);
    else await d.dismiss();
  });

  // Что прогон создал сам и должен убрать (даже если проверка упала).
  // token/clipFile — для сигналов тестового экрана; bc — состояние общего эфира до шага «Эфир сети».
  const fx = { screenId: null, mediaId: null, token: null, clipFile: null, bc: null };

  try {
    // ── 1. Вход ────────────────────────────────────────────────────────────
    head('Вход в панель');
    step = 'вход';
    await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 30000 });
    // Буфер обмена: по HTTP на IP контекст небезопасный, navigator.clipboard
    // недоступен и панель сама уходит на запасной execCommand. Разрешение
    // запрашиваем «если дадут» — на такой странице Chrome его не выдаёт, и это
    // нормально: проверять нужно именно поведение по HTTP, как на объекте.
    await browser.defaultBrowserContext()
      .overridePermissions(BASE, ['clipboard-read', 'clipboard-write'])
      .catch(() => {});
    await page.waitForSelector('#lg-pass', { timeout: 15000 });
    await page.$eval('#lg-user', (el, v) => { el.value = v; }, USER);
    await page.$eval('#lg-pass', (el, v) => { el.value = v; }, PASS);
    await page.click('[data-action="auth-login"]');
    try {
      await page.waitForSelector('.nav[data-nav="screens"]', { timeout: 15000 });
      ok(`вошли под «${USER}»`);
    } catch (e) {
      const err = await page.$eval('#lg-err', el => el.textContent).catch(() => '');
      bad(`вход не удался${err ? ' — ' + err : ''}`);
      throw new Error('без входа продолжать нечего');
    }

    // ── 1а. Тестовые объекты ───────────────────────────────────────────────
    // Плейлист не отправить без экрана и не собрать без одобренного ролика, а
    // на чистом стенде нет ни того, ни другого. Экран живёт до конца прогона
    // (на нём же проверяется бейдж монитора), ролик — до уборки.
    head('Подготовка: тестовый экран и ролик');
    step = 'подготовка';
    const scr = await apiCall(page, 'POST',
      `/minipc/register?name=${encodeURIComponent(FIXTURE_SCREEN)}&city=E2E`);
    if (scr.status === 200 && scr.data && scr.data.id) {
      fx.screenId = scr.data.id;
      fx.token = scr.data.token;
      ok(`тестовый экран заведён (id ${fx.screenId})`);
    } else {
      bad(`тестовый экран не заведён (HTTP ${scr.status})`);
    }
    // category=service — служебный контент: одобряется сразу, без модерации
    const clip = await apiCall(page, 'POST',
      `/media/upload?title=${encodeURIComponent(CLIP_TITLE)}&category=service&allow_duplicate=true`,
      { file: { b64: makePng(STAMP).toString('base64'), type: 'image/png', name: `e2e_${STAMP}.png` } });
    if (clip.status === 200 && clip.data && clip.data.id && clip.data.review_status === 'approved') {
      fx.mediaId = clip.data.id;
      fx.clipFile = clip.data.filename;
      ok(`тестовый ролик загружен и одобрен (id ${fx.mediaId})`);
    } else {
      if (clip.data && clip.data.id) fx.mediaId = clip.data.id;       // чтобы уборка его нашла
      bad(`тестовый ролик не загружен (HTTP ${clip.status})`);
    }

    // ── 2. Все разделы меню открываются ────────────────────────────────────
    head('Разделы меню');
    for (const [nav, title] of SECTIONS) {
      step = 'раздел ' + title;
      try {
        await gotoSection(page, nav);
        const shown = await page.$eval('#vtitle', el => el.textContent.trim());
        const errText = await page.$eval('#view', el => el.innerText).catch(() => '');
        if (shown !== title) bad(`${title}: в шапке «${shown}»`);
        else if (/^Ошибка:/m.test(errText)) bad(`${title}: вью отдало ошибку — ${errText.split('\n')[0]}`);
        else ok(`${title} открылся`);
      } catch (e) {
        bad(`${title}: ${e.message.split('\n')[0]}`);
      }
    }

    // ── 3. Слепой прогон безопасных кнопок ─────────────────────────────────
    head('Кнопки разделов (безопасные)');
    dialogMode = 'dismiss';
    for (const [nav, title] of SECTIONS) {
      await gotoSection(page, nav);
      const buttons = await safeButtons(page);
      if (!buttons.length) continue;
      // Нажимаем по одной, возвращаясь в раздел: клик мог открыть форму и
      // сменить содержимое, а нам нужен исходный список кнопок.
      const seen = {};
      let clicked = 0, failed = 0;
      for (const b of buttons) {
        const n = (seen[b.action] = (seen[b.action] ?? -1) + 1);
        step = `${title} → «${b.text || b.action}»`;
        const before = problems.length;
        try {
          if (!await clickNth(page, b.action, n)) continue;
          await sleep(500);
          clicked++;
          if (problems.length > before) failed++;
          await closeModals(page);       // «Просмотр» и подобные открывают окно — закрываем Esc
        } catch (e) {
          record('клик не прошёл', `${b.action}: ${e.message.split('\n')[0]}`);
          failed++;
        }
        await gotoSection(page, nav);
      }
      step = 'раздел ' + title;
      if (failed) bad(`${title}: нажато ${clicked}, с ошибками ${failed}`);
      else ok(`${title}: нажато кнопок ${clicked} — без ошибок`);
    }

    // ── 4. Экран: создать → найти → удалить ────────────────────────────────
    head('Цикл «экран»: создание и удаление');
    step = 'создание экрана';
    await gotoSection(page, 'screens');
    await page.click('[data-action="open-screen-add-form"]');
    await page.waitForSelector('#scr-name', { timeout: 10000 });
    await page.$eval('#scr-name', (el, v) => { el.value = v; }, SCREEN_NAME);
    await page.$eval('#scr-city', el => { el.value = 'E2E'; });
    await page.click('[data-action="submit-create-screen"]');
    try {
      await page.waitForSelector('[data-action="copy-screen-token"]', { timeout: 10000 });
      ok('экран создан, токен и ID показаны');
    } catch (e) {
      bad('экран не создался (нет блока с токеном)');
    }

    step = 'копирование токена';
    const beforeCopy = problems.length;
    await page.click('[data-action="copy-screen-token"]').catch(() => {});
    await sleep(400);
    if (problems.length > beforeCopy) bad('кнопка «Копировать» дала ошибку');
    else ok('токен копируется без ошибок');

    step = 'экран виден в списке';
    await gotoSection(page, 'screens');
    const listed = await page.evaluate(n => document.getElementById('view').innerText.includes(n), SCREEN_NAME);
    listed ? ok('экран появился в списке') : bad('созданного экрана нет в списке');

    step = 'удаление экрана';
    dialogMode = 'accept';
    const delBtn = await page.$(`[data-action="delete-screen"][data-screen-name="${SCREEN_NAME}"]`);
    if (!delBtn) {
      bad('кнопка удаления тестового экрана не найдена');
    } else {
      await delBtn.click();
      await sleep(1200);
      await gotoSection(page, 'screens');
      const still = await page.evaluate(n => document.getElementById('view').innerText.includes(n), SCREEN_NAME);
      still ? bad('экран остался в списке после удаления') : ok('экран удалён, список обновился');
    }

    // ── 5. Просмотр ролика в Медиатеке ─────────────────────────────────────
    head('Просмотр ролика');
    step = 'просмотр ролика';
    dialogMode = 'dismiss';
    try {
      await gotoSection(page, 'media');
      await page.click('[data-action="media-open-common"]');
      await page.waitForSelector('#view [data-action="mprev-open"]', { timeout: 10000 });
      await page.click('#view [data-action="mprev-open"]');
      const shown = await waitUntil(page,
        () => !!document.querySelector('.modal-mprev video, .modal-mprev img'), null, 10000);
      shown ? ok('клик по миниатюре открыл просмотр с видео или картинкой')
            : bad('просмотр ролика не открылся (нет ни <video>, ни <img>)');
      await page.keyboard.press('Escape');
      const closed = await waitUntil(page, () => !document.querySelector('.modal-backdrop'), null, 5000);
      closed ? ok('Esc закрыл просмотр') : bad('просмотр не закрылся по Esc');
    } catch (e) {
      bad('просмотр ролика: ' + e.message.split('\n')[0]);
      await closeModals(page);
    }

    // ── 5а. Дубль при загрузке: окно «Такой ролик уже есть» ────────────────
    // Тот же файл (совпадает md5), что и тестовый ролик, грузим через форму.
    // Сервер отвечает 409 — это ожидаемо; «Отмена» не должна оставить дубль.
    head('Дубль при загрузке');
    step = 'дубль при загрузке';
    const dupFile = `/tmp/e2e_dup_${STAMP}.png`;
    try {
      require('fs').writeFileSync(dupFile, makePng(STAMP));
      expected = [/^HTTP 409: .*\/api\/media\/upload/, /^console: .*status of 409/];
      await gotoSection(page, 'media');
      await page.click('[data-action="media-open-upload"]');
      await page.waitForSelector('#up-file', { timeout: 10000 });
      await page.waitForSelector('#up-cat option[value="service"]', { timeout: 10000 });
      await page.select('#up-cat', 'service');
      await (await page.$('#up-file')).uploadFile(dupFile);
      await page.type('#up-title', DUP_TITLE);
      await page.click('[data-action="media-submit-upload"]');
      const asked = await waitUntil(page, () => !!document.querySelector('.modal-mdup'), null, 15000);
      asked ? ok('повторная загрузка того же файла открыла окно про дубль')
            : bad('окно про дубль не открылось');
      const dupText = await page.$eval('.modal-mdup', el => el.innerText).catch(() => '');
      (dupText.includes('Такой ролик уже есть') && dupText.includes(CLIP_TITLE))
        ? ok('окно называет существующий ролик') : bad(`в окне про дубль нет названия ролика: «${dupText.slice(0, 80)}»`);
      const btns = await page.$$eval('.modal-mdup .modal-foot button', els => els.map(b => b.textContent.trim()));
      (btns.includes('Отмена') && btns.includes('Всё равно загрузить') && btns.includes('Открыть существующий'))
        ? ok('в окне три кнопки: Отмена, Всё равно загрузить, Открыть существующий')
        : bad(`кнопки окна про дубль: ${btns.join(' | ')}`);
      await page.click('.modal-mdup [data-mdup="cancel"]');
      (await waitUntil(page, () => !document.querySelector('.modal-backdrop'), null, 5000))
        ? ok('«Отмена» закрыла окно') : bad('«Отмена» не закрыла окно');
      const progress = await page.$eval('#up-progress', el => el.textContent).catch(() => '');
      /не загружен/.test(progress) ? ok('форма сообщила: файл не загружен')
                                   : bad(`форма не сообщила об отказе: «${progress}»`);
      const after = await apiCall(page, 'GET', '/media');
      const extra = (after.data || []).filter(m => m.title === DUP_TITLE);
      extra.length === 0 ? ok('после «Отмена» дубля в медиатеке нет')
                         : bad('после «Отмена» дубль остался в медиатеке (уберёт уборка)');
    } catch (e) {
      bad('дубль при загрузке: ' + e.message.split('\n')[0]);
      await closeModals(page);
    } finally {
      expected = [];
      try { require('fs').unlinkSync(dupFile); } catch (_) { /* файла могло не быть */ }
    }

    // ── 6. Плейлист целиком: окно → повторы → «Куда и когда» → карточка ────
    // Тот же путь, что у человека: «+ Плейлист» → название → поиск → галочка →
    // повторы ×N (предупреждение «подряд») → «Создать и отправить» → свой
    // тестовый экран, завтра, 10–11 ч → «Отправить». Дальше — карточка плейлиста
    // и «По датам» в Расписании, потом отмена показа и удаление плейлиста.
    // Только свой экран, не «Все экраны сети»: при жёстком обрыве прогона
    // (уборка не выполнится) показ не уйдёт на настоящие экраны стека.
    head('Цикл «плейлист»: окно, повторы, «Куда и когда»');
    const tomorrow = addDays(mskToday(), 1);
    let plId = null;
    try {
      step = 'окно «Новый плейлист»';
      dialogMode = 'dismiss';
      await gotoSection(page, 'playlists');
      await page.click('[data-action="playlists-create"]');
      await page.waitForSelector('#pled-name', { timeout: 10000 });
      ok('окно «Новый плейлист» открылось');

      step = 'плейлист: название и поиск';
      await page.type('#pled-name', PLAYLIST_NAME);
      await page.type('#pled-search', CLIP_QUERY);
      const found = await waitUntil(page,
        () => /найдено/.test(document.getElementById('pled-found').textContent), null, 8000);
      found ? ok('поиск по части названия нашёл ролик') : bad('поиск не нашёл тестовый ролик');

      step = 'плейлист: галочка и повторы';
      const tiles = await page.$$('.pled-tile .pled-pick[data-action="pled-toggle"]');
      if (!tiles.length) throw new Error('в окне нет ни одного выбираемого ролика');
      await tiles[0].click();
      const picked = await page.evaluate(() => ({
        sel: document.querySelectorAll('.pled-tile.sel').length,
        num: (document.querySelector('.pled-tile.sel .pled-chk') || {}).textContent,
      }));
      (picked.sel === 1 && picked.num === '1')
        ? ok('клик по плитке поставил галочку №1')
        : bad(`галочка не встала (выбрано ${picked.sel}, номер «${picked.num}»)`);
      for (let i = 0; i < 4; i++) await page.click('.pled-tile.sel [data-action="pled-rep-plus"]');
      const rep = await page.$eval('.pled-tile.sel .pled-rep b', el => el.textContent);
      rep === '×5' ? ok('«+» ×4 довело повторы до ×5') : bad(`повторы показывают «${rep}», ожидалось ×5`);
      const warned = await waitUntil(page, () => {
        const w = document.getElementById('pled-warns');
        return !!w && !w.hidden && /подряд/.test(w.textContent);
      }, null, 8000);
      warned ? ok('появилось предупреждение «подряд»') : bad('предупреждения «подряд» нет');
      const labels = await page.evaluate(() => ({
        save: document.getElementById('pled-save').textContent.trim(),
        sum: document.getElementById('pled-sum').textContent.trim(),
      }));
      /всё равно/.test(labels.save) ? ok(`кнопка предупреждает: «${labels.save}»`)
                                    : bad(`кнопка без «всё равно»: «${labels.save}»`);
      /5 показов/.test(labels.sum) ? ok('итог считает показы с повторами')
                                   : bad(`итог не посчитал 5 показов: «${labels.sum}»`);

      step = 'плейлист: создать и отправить';
      await page.click('#pled-save-send');
      await page.waitForSelector('#plc-date-from', { timeout: 10000 });
      ok('«Создать и отправить →» открыл шаг «Куда и когда»');
      const lst = await apiCall(page, 'GET', '/playlists');
      const made = (lst.data || []).find(p => p.name === PLAYLIST_NAME);
      made ? (plId = made.id, ok(`плейлист создан (id ${plId})`)) : bad('плейлиста нет в списке после «Создать»');

      step = 'куда и когда';
      if (!fx.screenId) throw new Error('нет тестового экрана — отправлять показ некуда');
      await page.evaluate(() =>
        document.querySelector('input[data-action="plc-target-pick"]').click());
      const scrBox = `input[data-action="plc-screen"][data-screen-id="${fx.screenId}"]`;
      await page.waitForSelector(scrBox, { timeout: 10000 });
      await page.evaluate(sel => { const el = document.querySelector(sel); if (!el.checked) el.click(); }, scrBox);
      const target = await page.evaluate(() => ({
        all: document.querySelector('input[data-action="plc-target-all"]').checked,
        picked: [...document.querySelectorAll('input[data-action="plc-screen"]:checked')]
          .map(el => Number(el.dataset.screenId)),
      }));
      (!target.all && target.picked.length === 1 && target.picked[0] === fx.screenId)
        ? ok('выбран только свой тестовый экран')
        : bad(`цель показа не та: все=${target.all}, отмечены ${target.picked.join(', ') || 'никакие'}`);
      await page.select('#plc-hour-from', '10');
      await page.select('#plc-hour-to', '11');
      for (const id of ['#plc-date-from', '#plc-date-to']) {
        await page.$eval(id, (el, v) => {
          el.value = v;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        }, tomorrow);
      }
      const previewed = await waitUntil(page, () => {
        const r = document.getElementById('plc-result');
        return !!r && !r.hidden && !r.classList.contains('plc-stale');
      }, null, 12000);
      previewed ? ok('предпросмотр пересечений пришёл') : bad('предпросмотр пересечений не пришёл');

      // Пересечения на чистом стенде не ожидаются; если есть — «Заменить на это время»
      // (и «ко всем», когда пересечений два и больше — тогда флажок есть в окне).
      const nConf = (await page.$$('.plc-conf')).length;
      if (nConf) {
        step = 'куда и когда: пересечения';
        console.log(`    пересечений: ${nConf} — выбираем «Заменить»`);
        const first = await page.$('input[data-action="plc-decide"][data-mode="replace"]');
        await page.evaluate(el => el.click(), first);
        if (await page.$('#plc-apply-all')) {
          await page.evaluate(() => document.getElementById('plc-apply-all').click());
        }
        for (const r of await page.$$('input[data-action="plc-decide"][data-mode="replace"]')) {
          if (!await page.evaluate(el => el.checked, r)) await page.evaluate(el => el.click(), r);
        }
      }
      const canSend = await waitUntil(page, () => !document.getElementById('plc-send').disabled, null, 8000);
      canSend ? ok('«Отправить на экраны» доступна') : bad('«Отправить на экраны» осталась недоступной');

      step = 'куда и когда: отправка';
      await page.click('#plc-send');
      const modalGone = await waitUntil(page, () => !document.querySelector('.modal-backdrop'), null, 10000);
      if (modalGone) {
        ok('показ отправлен, окно закрылось');
      } else {
        const err = await page.$eval('#plc-err', el => el.textContent.trim()).catch(() => '');
        bad(`окно «Куда и когда» не закрылось после отправки${err ? ' — ' + err : ''}`);
        await closeModals(page);
      }

      // ── карточка плейлиста: «Где и когда идёт» ──
      step = 'карточка плейлиста';
      await gotoSection(page, 'playlists');
      await page.click(`[data-action="playlists-open"][data-playlist-name="${PLAYLIST_NAME}"]`);
      await page.waitForSelector('#pl-placements', { timeout: 10000 });
      const planned = await waitUntil(page, () => {
        const b = document.getElementById('pl-placements');
        return !!b && /запланирован/.test(b.innerText);
      }, null, 10000);
      planned ? ok('в карточке строка «запланирован»') : bad('в карточке нет строки «запланирован»');
      const rowText = await page.$eval('#pl-placements', el => el.innerText.replace(/\s+/g, ' '));
      (rowText.includes(FIXTURE_SCREEN) && !/Все экраны сети/.test(rowText) && /10:00 – 11:00/.test(rowText))
        ? ok('в строке свой тестовый экран, 10:00 – 11:00')
        : bad(`строка показа не та: «${rowText.slice(0, 120)}»`);

      step = 'карточка: изменить ролики';
      await page.click('[data-action="playlists-edit-items"]');
      await page.waitForSelector('#pled-name', { timeout: 10000 });
      const edit = await page.evaluate(() => ({
        name: document.getElementById('pled-name').value,
        sel: document.querySelectorAll('.pled-tile.sel').length,
        rep: (document.querySelector('.pled-tile.sel .pled-rep b') || {}).textContent,
      }));
      (edit.name === PLAYLIST_NAME && edit.sel === 1 && edit.rep === '×5')
        ? ok('«Изменить ролики» открыл окно с названием, роликом и ×5')
        : bad(`окно правки открылось не так: название «${edit.name}», выбрано ${edit.sel}, повторы «${edit.rep}»`);
      await page.keyboard.press('Escape');
      (await waitUntil(page, () => !document.querySelector('.modal-backdrop'), null, 5000))
        ? ok('окно правки без изменений закрылось по Esc') : bad('окно правки не закрылось по Esc');

      step = 'карточка: отправить на экраны';
      await page.click('[data-action="playlists-send"]');
      (await waitUntil(page, () => !!document.getElementById('plc-date-from'), null, 10000))
        ? ok('«📤 Отправить на экраны» открыла «Куда и когда»') : bad('«Отправить на экраны» не открыла окно');
      await page.keyboard.press('Escape');
      (await waitUntil(page, () => !document.querySelector('.modal-backdrop'), null, 5000))
        ? ok('окно отправки закрылось по Esc') : bad('окно отправки не закрылось по Esc');

      // ── Расписание → «По датам» ──
      step = 'расписание: По датам';
      const gridReady = () => waitUntil(page, () => {
        const g = document.getElementById('sch-d-grid');
        return !!g && !g.classList.contains('busy') && !!g.querySelector('.schd-tbl');
      }, null, 12000);
      await gotoSection(page, 'schedule');
      await page.click('[data-action="schedule-target-screen"]');
      await page.waitForSelector('select[data-action="schedule-select-target"]', { timeout: 10000 });
      await page.select('select[data-action="schedule-select-target"]', String(fx.screenId));
      await sleep(800);
      await page.click('[data-action="schedule-mode-dates"]');
      (await gridReady()) ? ok('«По датам»: недельная сетка отрисована') : bad('«По датам»: сетка не отрисовалась');

      for (const [a, label] of [['schedule-week-next', '›'], ['schedule-week-prev', '‹']]) {
        await page.click(`[data-action="${a}"]`);
        (await gridReady()) ? ok(`«${label}» перелистнула неделю`) : bad(`«${label}»: сетка не обновилась`);
      }
      const cellSel = `[data-action="schedule-cell-open"][data-date="${tomorrow}"][data-hour="10"]`;
      let cell = await page.$(cellSel);
      if (!cell) {                                   // завтра — уже следующая неделя (сегодня воскресенье)
        await page.click('[data-action="schedule-week-next"]');
        await gridReady();
        cell = await page.$(cellSel);
      }
      if (!cell) {
        bad(`в сетке нет клетки показа на ${tomorrow} в 10:00`);
      } else {
        const cellText = await page.evaluate(el => el.textContent, cell);
        cellText.includes(PLAYLIST_NAME) ? ok('завтра в 10:00 стоит клетка показа плейлиста')
                                         : bad(`клетка показа не с тем плейлистом: «${cellText}»`);
        await cell.click();
        const card = await waitUntil(page, n => {
          const c = document.querySelector('#sch-d-card .schd-card');
          return !!c && c.innerText.includes(n) && /запланирован/.test(c.innerText);
        }, PLAYLIST_NAME, 10000);
        card ? ok('клик по клетке открыл карточку показа («запланирован»)') : bad('карточка показа не открылась');
        await page.click('[data-action="schedule-plc-close"]').catch(() => {});
        (await waitUntil(page, () => !document.querySelector('#sch-d-card .schd-card'), null, 5000))
          ? ok('карточка показа закрылась') : bad('карточка показа не закрылась');
      }
      await page.click('[data-action="schedule-week-today"]');
      (await gridReady()) ? ok('«Сегодня» вернуло текущую неделю') : bad('«Сегодня»: сетка не обновилась');
      // вернуть Расписание в исходный вид — дальше им пользуются другие шаги
      await page.click('[data-action="schedule-mode-week"]');
      await waitUntil(page, () => !!document.querySelector('#topright [data-action="schedule-mode-week"].on'), null, 8000);
      await page.click('[data-action="schedule-target-network"]');
      await waitUntil(page, () => !!document.querySelector('#topright [data-action="schedule-target-network"].on'), null, 8000);
      await waitViewReady(page);

      // ── отмена показа и удаление плейлиста ──
      step = 'отмена показа';
      dialogMode = 'accept';
      await gotoSection(page, 'playlists');
      await page.click(`[data-action="playlists-open"][data-playlist-name="${PLAYLIST_NAME}"]`);
      await page.waitForSelector('[data-action="playlists-plc-cancel"]', { timeout: 10000 });
      await page.click('[data-action="playlists-plc-cancel"]');
      const gone = await waitUntil(page, () => {
        const b = document.getElementById('pl-placements');
        return !!b && /пока не отправлен/.test(b.innerText);
      }, null, 10000);
      gone ? ok('«Отменить» убрала показ из карточки') : bad('после «Отменить» показ остался в карточке');

      step = 'удаление плейлиста';
      await gotoSection(page, 'playlists');
      const del = await page.$(`[data-action="playlists-delete"][data-playlist-name="${PLAYLIST_NAME}"]`);
      if (!del) {
        bad('кнопка удаления тестового плейлиста не найдена');
      } else {
        await del.click();
        await sleep(1200);
        await gotoSection(page, 'playlists');
        const still = await page.evaluate(n => document.getElementById('view').innerText.includes(n), PLAYLIST_NAME);
        still ? bad('плейлист остался в списке после удаления') : ok('плейлист удалён, список обновился');
      }
    } catch (e) {
      bad('цикл «плейлист» прерван на шаге «' + step + '»: ' + e.message.split('\n')[0]);
    } finally {
      dialogMode = 'dismiss';
      await closeModals(page).catch(() => {});
    }

    // ── 6а. Правило «в сети» и страница «Эфир сети» ────────────────────────
    // Своё: плейлист «E2E_эфир_…» с тестовым роликом и показ на ТЕКУЩИЙ час только на
    // собственный экран прогона — чтобы в таблице у него «сейчас идёт» была плашка.
    // Плейлист уходит в уборку (показ — вместе с ним). Общий эфир включается ТОЛЬКО
    // при E2E_ALLOW_BROADCAST=1: он идёт на все экраны сети; прежнее состояние
    // возвращается всегда (в конце шага и страховкой в общей уборке).
    head('Правило «в сети» и страница «Эфир сети»');
    const nl = { plId: null };
    try {
      if (!fx.screenId || !fx.mediaId || !fx.token) throw new Error('нет тестового экрана или ролика — проверять не на чем');
      dialogMode = 'dismiss';
      // Прежнее состояние эфира читаем ДО любых изменений и только из настоящего ответа: при сбое GET пустой
      // объект выглядел бы как «эфир выключен», и живой прежний эфир «вернулся» бы выключенным.
      const bcPrevR = await apiCall(page, 'GET', '/broadcast');
      if (bcPrevR.status !== 200) throw new Error(`GET /broadcast вернул HTTP ${bcPrevR.status} — прежнее состояние общего эфира неизвестно`);
      const bcPrev = bcPrevR.data || {};
      const bcOn = !!bcPrev.is_on;      // эфир уже включён до прогона: таблица показывает его, а не расписание
      if (bcOn && BC_ALLOWED) {
        // стенд в состоянии «эфир идёт»: выключаем на время прогона, в конце вернём как было
        fx.bc = { prev: bcPrev, dirty: true };
        const off = await apiCall(page, 'POST', '/broadcast/off');
        if (off.status !== 200) throw new Error(`не удалось выключить прежний эфир (HTTP ${off.status})`);
        console.log('    на стенде уже шёл общий эфир — выключен на время прогона, вернём в конце');
      } else if (bcOn) {
        console.log('    на стенде идёт общий эфир: проверки «источника» и окна включения пропускаются (E2E_ALLOW_BROADCAST не задан)');
      }
      const noBc = !bcOn || BC_ALLOWED;   // общий эфир сейчас не мешает проверкам «сейчас идёт»

      // ── «в сети»: одно правило в «Экранах» и на дашборде ──
      step = 'в сети: экран без сигнала';
      const cellsOf = async (nav) => {
        await gotoSection(page, nav);
        return page.evaluate(n => [...document.querySelectorAll('#view .cell')]
          .filter(el => el.innerText.includes(n)).map(el => el.innerText.replace(/\s+/g, ' ')), FIXTURE_SCREEN);
      };
      const NAVS = [['screens', 'Экраны'], ['dash', 'Дашборд']];
      for (const [nav, title] of NAVS) {
        const t = await cellsOf(nav);
        if (!t.length) bad(`${title}: тестового экрана нет в разделе`);
        else if (t.every(x => /ни разу не выходил на связь/.test(x))) ok(`${title}: экран без сигнала — «ни разу не выходил на связь»`);
        else bad(`${title}: экран без сигнала показан не так: «${t[0].slice(0, 100)}»`);
      }
      step = 'в сети: сигнал получен';
      const hb = await heartbeat(page, fx.screenId, fx.token, fx.clipFile);
      hb === 200 ? ok('сигнал тестового экрана принят (X-Token, как у агента)') : bad(`сигнал тестового экрана: HTTP ${hb}`);
      for (const [nav, title] of NAVS) {
        const t = await cellsOf(nav);
        if (!t.length) bad(`${title}: тестового экрана нет в разделе`);
        else if (t.every(x => !/ни разу не выходил|не в сети/.test(x))) ok(`${title}: после сигнала экран в сети`);
        else bad(`${title}: после сигнала экран всё ещё не в сети: «${t[0].slice(0, 100)}»`);
      }

      // ── свой плейлист и показ на текущий час ──
      step = 'эфир сети: плейлист и показ';
      const mk = await apiCall(page, 'POST', '/playlists/full',
        { json: { name: NL_PLAYLIST, items: [{ media_id: fx.mediaId }] } });
      if (mk.status !== 200 || !mk.data || !mk.data.id) throw new Error(`плейлист для «Эфира сети» не создан (HTTP ${mk.status})`);
      nl.plId = mk.data.id;
      const now0 = await serverNow(page);
      const hFrom = now0.hour, hTo = Math.min(hFrom + 2, 24);      // два часа — запас на стык часов
      const plcBody = { playlist_id: nl.plId, screen_ids: [fx.screenId],
                        date_from: now0.date, date_to: now0.date, hour_from: hFrom, hour_to: hTo };
      const pv = await apiCall(page, 'POST', '/placements/preview', { json: plcBody });
      const decisions = ((pv.data || {}).conflicts || []).map(c => (
        { screen_id: c.screen_id, other_kind: c.other_kind, other_id: c.other_id, mode: 'replace' }));
      const plc = await apiCall(page, 'POST', '/placements', { json: { ...plcBody, decisions } });
      if (plc.status !== 200) throw new Error(`показ на текущий час не создан (HTTP ${plc.status})`);
      ok(`плейлист «${NL_PLAYLIST}» и показ на ${hh(hFrom)}:00 – ${hh(hTo)}:00 только на свой экран созданы`);

      // ── таблица ──
      step = 'эфир сети: таблица';
      await heartbeat(page, fx.screenId, fx.token, fx.clipFile);
      await gotoSection(page, 'broadcast');
      const rowSel = `#nl-main tr[data-screen-id="${fx.screenId}"]`;
      await page.waitForSelector(rowSel, { timeout: 15000 });
      ok('страница «Эфир сети» открылась, в таблице есть тестовый экран');
      const stat = await page.$eval('#nl-stat', el => el.innerText.replace(/\s+/g, ' '));
      (/\d+ экран/.test(stat) && /\d+ в сети/.test(stat) && /\d+ не в сети/.test(stat) && /обновлено \d\d:\d\d:\d\d/.test(stat))
        ? ok(`шапка: «${stat}»`) : bad(`шапка страницы не та: «${stat}»`);
      const heads = await page.$$eval('#nl-main thead th', els => els.map(e => e.textContent.trim()));
      const lastHead = noBc ? 'Дальше' : 'После эфира';     // «После эфира» — только пока идёт общий эфир
      (heads.join('|') === `Экран|Сейчас идёт|Играет ролик|${lastHead}`)
        ? ok(`колонки таблицы: ${heads.join(' · ')}`) : bad(`колонки таблицы: ${heads.join(' · ')}`);
      const row = await page.evaluate(sel => {
        const tr = document.querySelector(sel);
        return {
          text: tr.innerText.replace(/\s+/g, ' '),
          dot: (tr.querySelector('.nl-dot') || document.body).getAttribute('aria-label') || '',
          plaques: [...tr.querySelectorAll('[data-action="nl-open-playlist"]')]
            .map(b => ({ id: Number(b.dataset.playlistId), name: b.dataset.playlistName })),
          clip: (tr.querySelector('[data-action="nl-preview-clip"]') || {}).innerText || '',
        };
      }, rowSel);
      (row.text.includes(FIXTURE_SCREEN) && row.dot === 'в сети')
        ? ok('в строке экрана: название, точка «в сети»') : bad(`строка экрана не та: «${row.text.slice(0, 100)}», точка «${row.dot}»`);
      row.plaques.some(p => p.id === nl.plId && p.name === NL_PLAYLIST)
        ? ok('у экрана плашка его плейлиста') : bad(`в строке нет плашки плейлиста «${NL_PLAYLIST}»`);
      row.clip.includes(CLIP_TITLE) ? ok('«Играет ролик»: название ролика, который сообщил экран')
                                    : bad(`«Играет ролик» не показал ролик «${CLIP_TITLE}»: «${row.clip}»`);
      if (noBc) {
        /📅 показ/.test(row.text) ? ok('источник: «📅 показ»') : bad(`источник строки не «📅 показ»: «${row.text.slice(0, 140)}»`);
      }

      // ── поиск ──
      step = 'эфир сети: поиск';
      const setSearch = async (v) => {
        await page.$eval('#nl-search', (el, val) => {
          el.value = val; el.dispatchEvent(new Event('input', { bubbles: true }));
        }, v);
        await sleep(200);
      };
      const rowCount = () => page.$$eval('#nl-main tbody tr', els => els.length);
      await setSearch(FIXTURE_SCREEN);
      const byName = await page.$$eval('#nl-main tbody tr', els => els.map(e => Number(e.dataset.screenId)));
      const foundTxt = await page.$eval('#nl-stat', el => el.innerText.replace(/\s+/g, ' '));
      (byName.length === 1 && byName[0] === fx.screenId && /найдено 1/.test(foundTxt))
        ? ok('поиск по названию экрана оставил одну строку, в шапке «найдено 1»')
        : bad(`поиск по названию: строк ${byName.length}, шапка «${foundTxt}»`);
      await setSearch(NL_PLAYLIST.toUpperCase());
      const byPl = await page.$$eval('#nl-main tbody tr', els => els.map(e => Number(e.dataset.screenId)));
      byPl.includes(fx.screenId) ? ok('поиск по названию плейлиста (без учёта регистра) находит экран')
                                 : bad('поиск по названию плейлиста не нашёл экран');
      await setSearch('нет_такого_' + STAMP);
      const none = await page.$eval('#nl-main', el => el.innerText);
      /Ничего не найдено по запросу/.test(none) ? ok('пустой поиск: «Ничего не найдено по запросу…»')
                                                : bad(`пустой поиск без подсказки: «${none.slice(0, 80)}»`);
      await setSearch('');
      (await rowCount()) >= 1 && (await page.$(rowSel)) ? ok('очистка поиска вернула таблицу') : bad('после очистки поиска таблица не вернулась');

      // ── панель «О плейлисте» ──
      step = 'эфир сети: панель плейлиста';
      const plaqueSel = `${rowSel} [data-action="nl-open-playlist"][data-playlist-id="${nl.plId}"]`;
      await page.click(plaqueSel);
      const drawerReady = await waitUntil(page, (n) => {
        const d = document.getElementById('nl-drawer');
        // textContent, а не innerText: подписи разделов в панели набраны капсом (text-transform)
        return !!d && !d.hidden && d.textContent.includes(n[0]) && /Где и когда идёт/.test(d.textContent);
      }, [CLIP_TITLE], 12000);
      drawerReady ? ok('клик по плашке открыл панель «О плейлисте» с роликами и показами') : bad('панель плейлиста не открылась или не загрузилась');
      const dr = await page.evaluate(() => ({
        title: (document.getElementById('nl-drawer-title') || {}).textContent || '',
        sub: (document.querySelector('#nl-drawer .nl-sub') || {}).textContent || '',
        text: document.getElementById('nl-drawer').textContent.replace(/\s+/g, ' '),
        sel: !!document.querySelector('#nl-main .nl-pl.sel'),
      }));
      dr.title === NL_PLAYLIST ? ok('в панели название плейлиста') : bad(`заголовок панели «${dr.title}»`);
      /^1 ролик · 1 показ · \d+:\d\d · (как есть|блок \d+ мин с заглушками)$/.test(dr.sub.trim())
        ? ok(`сводка панели: «${dr.sub.trim()}»`) : bad(`сводка панели не та: «${dr.sub}»`);
      dr.sel ? ok('плашка открытого плейлиста подсвечена') : bad('плашка открытого плейлиста не подсвечена');
      if (noBc) {
        /Сейчас идёт на 1 экране/.test(dr.text) && dr.text.includes(FIXTURE_SCREEN)
          ? ok('панель: «Сейчас идёт на 1 экране» — тестовый экран') : bad(`панель не показала «Сейчас идёт на 1 экране»: «${dr.text.slice(0, 160)}»`);
      }
      (dr.text.includes(FIXTURE_SCREEN) && dr.text.includes(`${hh(hFrom)}–${hh(hTo)} ч`) && /идёт сейчас/.test(dr.text))
        ? ok(`«Где и когда идёт»: показ на свой экран, ${hh(hFrom)}–${hh(hTo)} ч, «идёт сейчас»`)
        : bad(`«Где и когда идёт» не сошлось: «${dr.text.slice(0, 200)}»`);

      step = 'эфир сети: просмотр ролика из панели';
      await page.click('#nl-drawer [data-action="nl-preview-clip"]');
      (await waitUntil(page, () => !!document.querySelector('.modal-mprev video, .modal-mprev img'), null, 10000))
        ? ok('клик по ролику в панели открыл просмотр') : bad('просмотр ролика из панели не открылся');
      await page.keyboard.press('Escape');
      (await waitUntil(page, () => !document.querySelector('.modal-backdrop'), null, 5000))
        ? ok('Esc закрыл просмотр') : bad('просмотр не закрылся по Esc');
      (await page.$('#nl-drawer:not([hidden])')) ? ok('панель плейлиста осталась открытой') : bad('Esc закрыл заодно и панель плейлиста');

      step = 'эфир сети: правка плейлиста поверх страницы';
      await page.click('#nl-drawer [data-action="nl-edit-playlist"]');
      await page.waitForSelector('#pled-name', { timeout: 10000 });
      const ed = await page.evaluate(() => ({
        name: document.getElementById('pled-name').value,
        sel: document.querySelectorAll('.pled-tile.sel').length,
        info: (document.querySelector('.pled-info') || {}).innerText || '',
      }));
      (ed.name === NL_PLAYLIST && ed.sel === 1)
        ? ok('«✎ Изменить плейлист» открыл окно правки с названием и роликом') : bad(`окно правки не то: название «${ed.name}», выбрано ${ed.sel}`);
      if (noBc) {
        /Плейлист сейчас идёт на 1 экране — изменения дойдут до него в течение минуты/.test(ed.info.replace(/\s+/g, ' '))
          ? ok('в окне подсказка «идёт на 1 экране — изменения дойдут до него в течение минуты»') : bad(`подсказки про экраны нет: «${ed.info}»`);
      }
      await page.keyboard.press('Escape');
      (await waitUntil(page, () => !document.querySelector('.modal-backdrop'), null, 5000))
        ? ok('Esc закрыл окно правки без сохранения') : bad('окно правки не закрылось по Esc');
      (await page.$('#nl-root')) && (await page.$('#nl-drawer:not([hidden])'))
        ? ok('пользователь остался на «Эфире сети», панель на месте') : bad('после правки страница или панель пропали');

      step = 'эфир сети: отправка на экраны из панели';
      await page.click('#nl-drawer [data-action="nl-send-playlist"]');
      (await waitUntil(page, () => !!document.getElementById('plc-date-from'), null, 10000))
        ? ok('«📤 Отправить на экраны» открыла «Куда и когда»') : bad('«Отправить на экраны» не открыла окно');
      await page.keyboard.press('Escape');
      (await waitUntil(page, () => !document.querySelector('.modal-backdrop'), null, 5000))
        ? ok('окно «Куда и когда» закрылось по Esc') : bad('окно «Куда и когда» не закрылось по Esc');

      step = 'эфир сети: закрытие панели';
      await page.click('#nl-drawer [data-action="nl-close-drawer"]');
      (await waitUntil(page, () => document.getElementById('nl-drawer').hidden, null, 5000))
        ? ok('✕ закрыл панель') : bad('✕ не закрыл панель');
      const back = await page.evaluate(() => (document.activeElement || {}).dataset && document.activeElement.dataset.action);
      back === 'nl-open-playlist' ? ok('фокус вернулся на плашку плейлиста') : bad(`фокус после закрытия панели на «${back}»`);
      await page.click(plaqueSel);
      await waitUntil(page, () => !document.getElementById('nl-drawer').hidden, null, 5000);
      await page.keyboard.press('Escape');
      (await waitUntil(page, () => document.getElementById('nl-drawer').hidden, null, 5000))
        ? ok('Esc закрывает панель') : bad('Esc не закрыл панель');

      // ── полоса общего эфира и окно включения ──
      // окно: открыть кнопкой полосы, выбрать свой плейлист (серый выбрать нельзя)
      const openWindow = async (btn) => {
        await page.click(`#nl-broadcast [data-action="${btn}"]`);
        await page.waitForSelector('#bc-pls .bc-pl', { timeout: 10000 });
      };
      const pickMine = async () => {
        const label = await page.$(`#bc-pls .bc-pl[data-id="${nl.plId}"]`);
        if (!label) throw new Error('в окне эфира нет тестового плейлиста');
        const st = await page.evaluate(el => ({ dis: el.classList.contains('dis'),
          disabled: el.querySelector('input').disabled }), label);
        if (st.dis || st.disabled) throw new Error('тестовый плейлист в окне эфира серый (нечего играть)');
        await label.click();
        await waitUntil(page, n => (document.getElementById('bc-picked') || {}).innerText.includes(n), NL_PLAYLIST, 5000);
      };
      if (bcOn && !BC_ALLOWED) {
        console.log('  – полоса и окно общего эфира — пропущено (на стенде уже идёт эфир, E2E_ALLOW_BROADCAST не задан)');
      } else {
        step = 'общий эфир: полоса и окно';
        await gotoSection(page, 'broadcast');
        const offBar = await waitUntil(page,
          () => !!document.querySelector('#nl-broadcast .bc-bar.off [data-action="bc-open"]'), null, 10000);
        offBar ? ok('полоса: «Общий эфир выключен», кнопка «Включить общий эфир…»') : bad('полосы выключенного эфира нет');
        await openWindow('bc-open');
        const w1 = await page.evaluate(() => ({
          title: document.querySelector('.modal-bc .modal-title').textContent,
          untilOn: document.querySelector('input[data-action="bc-duration"][data-mode="until"]').checked,
          hours: [...document.querySelectorAll('#bc-until-hour option')].map(o => Number(o.value)),
          hour: Number(document.getElementById('bc-until-hour').value),
          submit: document.getElementById('bc-submit').disabled,
        }));
        w1.title === 'Включить общий эфир на все экраны' ? ok('окно «Включить общий эфир на все экраны» открылось') : bad(`заголовок окна эфира «${w1.title}»`);
        w1.submit ? ok('пока плейлист не выбран, «Включить» недоступна') : bad('«Включить» доступна без выбранного плейлиста');
        (w1.untilOn && w1.hours.length >= 1 && w1.hours[w1.hours.length - 1] === 24 && w1.hour === w1.hours[0] && w1.hours[0] > now0.hour)
          ? ok(`по умолчанию «до ${hh(w1.hour)}:00» (список часов: следующий … 24)`) : bad(`часы окна: по умолчанию ${w1.hour}, список ${w1.hours.join(',')}`);
        await pickMine();
        const w2 = await page.evaluate(() => ({
          warn: document.getElementById('bc-warn').innerText.replace(/\s+/g, ' '),
          submit: document.getElementById('bc-submit').innerText.trim(),
          off: document.getElementById('bc-submit').disabled,
        }));
        (/будет идти только этот плейлист/.test(w2.warn) && /Показы по датам, расписание и замены/.test(w2.warn))
          ? ok('жёлтое предупреждение: «… будет идти только этот плейлист. Показы по датам, расписание и замены … перекрываются»')
          : bad(`предупреждение окна не то: «${w2.warn}»`);
        (!w2.off && /^Включить на \d+ экран/.test(w2.submit)) ? ok(`кнопка: «${w2.submit}»`) : bad(`кнопка окна после выбора: «${w2.submit}», недоступна=${w2.off}`);
        await page.click('#bc-cancel');
        (await waitUntil(page, () => !document.querySelector('.modal-backdrop'), null, 5000))
          ? ok('«Отмена» закрыла окно') : bad('«Отмена» не закрыла окно');
        const still = (await apiCall(page, 'GET', '/broadcast')).data || {};
        (still.is_on === false) ? ok('после «Отмена» эфир не включился') : bad('после «Отмена» общий эфир включён!');
      }

      // ── включение эфира: только на стенде ──
      if (!BC_ALLOWED) {
        console.log('  – включение общего эфира на все экраны — пропущено (задайте E2E_ALLOW_BROADCAST=1 на стенде)');
      } else {
        step = 'общий эфир: включение';
        if (!fx.bc) fx.bc = { prev: bcPrev, dirty: false };
        // в последние секунды часа ждём следующий: эфир «до следующего часа» не должен кончиться посреди проверки
        const n1 = await serverNow(page);
        if (n1.minute === 59 && n1.second >= 30) {
          console.log('    конец часа — ждём следующий, чтобы эфир не кончился посреди проверки');
          await sleep((63 - n1.second) * 1000);
        }
        await gotoSection(page, 'broadcast');
        await heartbeat(page, fx.screenId, fx.token, fx.clipFile);
        await waitUntil(page, () => !!document.querySelector('#nl-broadcast .bc-bar.off'), null, 10000);
        fx.bc.dirty = true;                 // дальше эфир могут включить — вернуть прежнее в любом случае
        await openWindow('bc-open');
        await pickMine();
        const hourOf = () => page.$eval('#bc-until-hour', el => Number(el.value));
        let hour = await hourOf();
        await page.click('#bc-submit');
        let closed = await waitUntil(page, () => !document.querySelector('.modal-backdrop'), null, 10000);
        if (!closed) {
          const err = await page.$eval('#bc-err', el => el.textContent.trim()).catch(() => '');
          if (/Время изменилось/.test(err)) {      // час сменился между открытием и отправкой: окно пересобрало список
            hour = await hourOf();
            await page.click('#bc-submit');
            closed = await waitUntil(page, () => !document.querySelector('.modal-backdrop'), null, 10000);
          }
          if (!closed) {
            bad(`окно включения не закрылось${err ? ' — ' + err : ''}`);
            await closeModals(page);
          }
        }
        if (closed) ok(`«Включить на … экранах» отправила запрос, окно закрылось (до ${hh(hour)}:00)`);

        step = 'общий эфир: жёлтая полоса';
        const barOn = await waitUntil(page,
          () => !!document.querySelector('#nl-broadcast .bc-bar.on [data-action="bc-off"]'), null, 15000);
        barOn ? ok('полоса стала жёлтой (включён), есть «Сменить плейлист» и «Выключить»') : bad('жёлтой полосы нет после включения');
        const bar = await page.$eval('#nl-broadcast', el => el.innerText.replace(/\s+/g, ' ')).catch(() => '');
        (bar.includes(`«${NL_PLAYLIST}»`) && bar.includes(`до ${hh(hour)}:00`) && /осталось/.test(bar)
          && bar.includes('включил ' + USER) && /расписание на это время перекрыто/.test(bar))
          ? ok(`полоса: плейлист, «до ${hh(hour)}:00 (осталось …)», «включил ${USER}», «расписание … перекрыто»`)
          : bad(`текст полосы не тот: «${bar}»`);
        await waitUntil(page, () => [...document.querySelectorAll('#nl-main thead th')].some(t => t.textContent.trim() === 'После эфира'), null, 15000);
        const rowB = await page.$eval(rowSel, el => el.innerText.replace(/\s+/g, ' ')).catch(() => '');
        (rowB.includes(`общий эфир до ${hh(hour)}:00`))
          ? ok(`таблица: у экрана «📡 общий эфир до ${hh(hour)}:00», последняя колонка — «После эфира»`)
          : bad(`строка экрана при эфире не та: «${rowB.slice(0, 160)}»`);
        const feedOn = ((await apiCall(page, 'GET', '/dashboard')).data || {}).feed || [];
        feedOn.some(x => ['Общий эфир включён', 'Общий эфир сменён'].includes(x.title) && (x.detail || '').includes(NL_PLAYLIST)
                         && (x.detail || '').includes(`до ${hh(hour)}:00`))
          ? ok(`в журнале: «Общий эфир включён … до ${hh(hour)}:00»`) : bad('в журнале нет записи о включении эфира');

        step = 'общий эфир: окно «Сменить плейлист»';
        await openWindow('bc-change');
        const wc = await page.evaluate(() => ({
          title: document.querySelector('.modal-bc .modal-title').textContent,
          checked: Number((document.querySelector('#bc-pls input:checked') || {}).value),
          submit: document.getElementById('bc-submit').innerText.trim(),
        }));
        (wc.title === 'Сменить плейлист общего эфира' && wc.checked === nl.plId && wc.submit === 'Сменить плейлист')
          ? ok('окно «Сменить плейлист общего эфира»: выбран текущий плейлист, кнопка «Сменить плейлист»')
          : bad(`окно смены эфира не то: «${wc.title}», выбран #${wc.checked}, кнопка «${wc.submit}»`);
        await page.keyboard.press('Escape');
        (await waitUntil(page, () => !document.querySelector('.modal-backdrop'), null, 5000))
          ? ok('Esc закрыл окно смены без изменений') : bad('окно смены не закрылось по Esc');

        step = 'общий эфир: выключение';
        dialogMode = 'accept';             // confirm() «Выключить общий эфир?»
        await page.click('#nl-broadcast [data-action="bc-off"]');
        const barOff2 = await waitUntil(page, () => !!document.querySelector('#nl-broadcast .bc-bar.off'), null, 15000);
        dialogMode = 'dismiss';
        barOff2 ? ok('«Выключить» (с подтверждением): полоса снова серая') : bad('после «Выключить» полоса не стала серой');
        await waitUntil(page, () => [...document.querySelectorAll('#nl-main thead th')].some(t => t.textContent.trim() === 'Дальше'), null, 15000);
        const heads2 = await page.$$eval('#nl-main thead th', els => els.map(e => e.textContent.trim()));
        heads2[3] === 'Дальше' ? ok('последняя колонка снова «Дальше»') : bad(`колонки после выключения: ${heads2.join(' · ')}`);
        const offState = (await apiCall(page, 'GET', '/broadcast')).data || {};
        offState.is_on === false ? ok('GET /broadcast: эфир выключен') : bad('эфир остался включённым после «Выключить»');
        const feedOff = ((await apiCall(page, 'GET', '/dashboard')).data || {}).feed || [];
        feedOff.some(x => x.title === 'Общий эфир выключен') ? ok('в журнале: «Общий эфир выключен»') : bad('в журнале нет записи о выключении эфира');
      }
    } catch (e) {
      bad('«Эфир сети» прерван на шаге «' + step + '»: ' + e.message.split('\n')[0]);
    } finally {
      dialogMode = 'dismiss';
      await closeModals(page).catch(() => {});
      try { await restoreBroadcast(page, fx); } catch (e) { bad('эфир не возвращён: ' + e.message.split('\n')[0]); }
    }

    // ── 7. Карточка рекламодателя: все вкладки открываются ────────────────
    head('Карточка рекламодателя');
    step = 'открытие карточки';
    dialogMode = 'dismiss';
    await gotoSection(page, 'advertisers');
    const advCard = await page.$('[data-action="adv-open"]');
    if (!advCard) {
      bad('в списке нет ни одного рекламодателя — карточку проверить не на чем');
    } else {
      await advCard.click();
      await waitViewReady(page);
      const tabs = await page.$$eval('[data-action="adv-tab"]', els => els.map(e => e.dataset.tab));
      if (!tabs.length) bad('вкладки карточки не отрисовались');
      else ok(`карточка открылась, вкладок ${tabs.length}`);
      for (const t of tabs) {
        step = `карточка → вкладка ${t}`;
        const before = problems.length;
        await page.click(`[data-action="adv-tab"][data-tab="${t}"]`).catch(() => {});
        await sleep(900);
        const txt = await page.$eval('#view', el => el.innerText).catch(() => '');
        if (problems.length > before) bad(`вкладка «${t}»: ошибка при открытии`);
        else if (/^Ошибка:/m.test(txt)) bad(`вкладка «${t}» отдала ошибку`);
        else ok(`вкладка «${t}» открылась`);
      }
    }

    // ── 8. Роль «рекламодатель»: кабинет свой, остальное закрыто ──────────
    // Самая дорогая ошибка этого раздела — если рекламодатель увидит чужой
    // эфир или деньги. Проверяем на живой учётке и убираем её за собой.
    head('Изоляция роли «рекламодатель»');
    step = 'создание тестовой учётки';
    const advUser = `e2e_adv_${STAMP}`;
    const created = await page.evaluate(async (u) => {
      const r = await fetch('/api/users', {
        method: 'POST',
        headers: {'Content-Type': 'application/json',
                  'Authorization': 'Bearer ' + localStorage.getItem('ds_token')},
        body: JSON.stringify({username: u, password: 'e2e_secret_1', role: 'advertiser',
                              advertiser_name: 'E2E Рекламодатель ' + u}),
      });
      return r.ok ? await r.json() : {error: r.status};
    }, advUser);

    if (created.error) {
      bad(`не удалось создать учётку рекламодателя (HTTP ${created.error})`);
    } else if (!created.advertiser_id) {
      bad('кабинет не создался автоматически при заведении пользователя');
    } else {
      ok(`учётка создана, кабинет заведён автоматически (id ${created.advertiser_id})`);

      const ctx = await browser.createBrowserContext();
      const ap = await ctx.newPage();
      try {
        step = 'вход рекламодателем';
        await ap.goto(BASE, {waitUntil: 'networkidle2'});
        await ap.waitForSelector('#lg-pass');
        await ap.$eval('#lg-user', (e, v) => { e.value = v; }, advUser);
        await ap.$eval('#lg-pass', e => { e.value = 'e2e_secret_1'; });
        await ap.click('[data-action="auth-login"]');
        await ap.waitForSelector('.nav[data-nav="advertisers"]', {timeout: 15000});
        await sleep(1500);

        const navs = await ap.$$eval('.nav[data-nav]', els => els.map(e => e.dataset.nav));
        if (navs.length === 1 && navs[0] === 'advertisers') ok('в меню только его кабинет');
        else bad(`рекламодателю видно лишних разделов: ${navs.join(', ')}`);

        step = 'запрет чужих данных';
        const probe = await ap.evaluate(async (otherId) => {
          const t = localStorage.getItem('ds_token');
          const get = async (u) => (await fetch(u, {headers: {'Authorization': 'Bearer ' + t}})).status;
          return {
            list: await get('/api/advertisers'),
            screens: await get('/api/minipc'),
            invoices: await get('/api/billing/invoices'),
            users: await get('/api/users'),
            foreign: await get(`/api/advertisers/${otherId}/creatives`),
            own: await get('/api/advertisers/me'),
          };
        }, created.advertiser_id === 1 ? 2 : 1);

        const leaks = Object.entries(probe)
          .filter(([k, v]) => k !== 'own' && v !== 403)
          .map(([k, v]) => `${k}=${v}`);
        if (leaks.length) bad(`УТЕЧКА: доступно то, что должно быть закрыто — ${leaks.join(', ')}`);
        else ok('чужие данные и разделы закрыты (403)');
        if (probe.own === 200) ok('свой кабинет открывается');
        else bad(`свой кабинет недоступен (HTTP ${probe.own})`);
      } catch (e) {
        bad('проверка изоляции не завершилась: ' + e.message.split('\n')[0]);
      } finally {
        await ctx.close();
      }

      step = 'уборка тестовой учётки';
      // Удаляем и учётку, и созданный ею кабинет: удаление пользователя
      // намеренно НЕ трогает рекламодателя (в нём ролики, счета, документы),
      // поэтому тестовый кабинет убираем отдельно, иначе он копится от
      // прогона к прогону.
      const del = await page.evaluate(async (userId, advId) => {
        const h = {'Authorization': 'Bearer ' + localStorage.getItem('ds_token')};
        const u = await fetch('/api/users/' + userId, {method: 'DELETE', headers: h});
        const a = await fetch('/api/advertisers/' + advId, {method: 'DELETE', headers: h});
        return {user: u.status, adv: a.status};
      }, created.id, created.advertiser_id);
      (del.user === 200 && del.adv === 200)
        ? ok('тестовая учётка и её кабинет удалены')
        : bad(`уборка не прошла (учётка ${del.user}, кабинет ${del.adv})`);
    }

    // ── 9. Индикатор монитора (V2, миграция 028) ───────────────────────────
    head('Контроль монитора');
    step = 'бейдж монитора';
    await gotoSection(page, 'screens');
    const badge = await page.evaluate(() =>
      [...document.querySelectorAll('#view span[title]')].some(s => s.textContent.includes('📺')));
    badge ? ok('бейдж 📺 отрисован на карточках экранов')
          : bad('бейджа 📺 нет — проверьте, что /minipc отдаёт display_connected');

  } catch (e) {
    bad('прогон прерван: ' + e.message.split('\n')[0]);
  } finally {
    // ── Уборка: всё, что прогон создал сам, убирается ВСЕГДА — и когда проверка выше упала ──
    // Общий эфир — первым делом: пока он включён, он идёт на все экраны сети (страховка;
    // в норме шаг «Эфир сети» уже вернул прежнее состояние и здесь ничего не делает).
    try { await restoreBroadcast(page, fx); } catch (e) { bad('эфир не возвращён: ' + e.message.split('\n')[0]); }
    if (fx.screenId || fx.mediaId) {
      head('Уборка тестовых объектов');
      step = 'уборка';
      dialogMode = 'dismiss';
      try {
        await closeModals(page);
        // Плейлист ищем по названию: цикл мог упасть до удаления. Показы уходят вместе с ним.
        const pls = await apiCall(page, 'GET', '/playlists');
        for (const p of (pls.data || []).filter(p => p.name === PLAYLIST_NAME || p.name === NL_PLAYLIST)) {
          const r = await apiCall(page, 'DELETE', '/playlists/' + p.id);
          r.status === 200 ? ok(`тестовый плейлист «${p.name}» убран (вместе с показами)`)
                           : bad(`тестовый плейлист не удалился (HTTP ${r.status})`);
        }
        // Копия, если шаг про дубль всё же загрузил её (в норме её нет)
        const ml = await apiCall(page, 'GET', '/media');
        for (const m of (ml.data || []).filter(m => m.title === DUP_TITLE)) {
          const r = await apiCall(page, 'DELETE', '/media/' + m.id);
          r.status === 200 ? ok('лишняя копия ролика удалена') : bad(`копия ролика не удалилась (HTTP ${r.status})`);
        }
        if (fx.mediaId) {
          const r = await apiCall(page, 'DELETE', '/media/' + fx.mediaId);
          r.status === 200 ? ok('тестовый ролик удалён') : bad(`тестовый ролик не удалился (HTTP ${r.status})`);
        }
        if (fx.screenId) {
          const r = await apiCall(page, 'DELETE', '/minipc/' + fx.screenId);
          r.status === 200 ? ok('тестовый экран удалён') : bad(`тестовый экран не удалился (HTTP ${r.status})`);
        }
      } catch (e) {
        bad('уборка не завершилась: ' + e.message.split('\n')[0]);
      }
    }
    await browser.close();
  }

  // ── Итог ────────────────────────────────────────────────────────────────
  if (problems.length) {
    console.log('\n▶ Ошибки консоли / HTTP (' + problems.length + ')');
    problems.slice(0, 40).forEach(p => console.log('  • ' + p));
    if (problems.length > 40) console.log(`  … и ещё ${problems.length - 40}`);
  }
  console.log('\n════════════════════════════════════════════');
  console.log(` Проверок пройдено: ${PASS_N}, провалено: ${FAIL_N}, ошибок в браузере: ${problems.length}`);
  console.log('════════════════════════════════════════════');
  process.exit(FAIL_N || problems.length ? 1 : 0);
})();
