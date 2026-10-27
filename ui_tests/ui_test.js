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
 *      повторы ×N, предупреждение «подряд»), шаг «Куда и когда» (все экраны
 *      сети, завтра, 10–11 ч, пересечения), строка «запланирован» в карточке,
 *      режим «По датам» в Расписании, отмена показа и удаление плейлиста.
 *      Плюс просмотр ролика в Медиатеке.
 *   5. Копит ошибки консоли, необработанные исключения и ответы HTTP ≥400.
 *
 * Любая ошибка консоли/JS и любой ответ ≥400 — это провал прогона: именно
 * так выглядели дефекты, которые ловили руками.
 *
 * Тестовые объекты. Для плейлиста нужны экран и одобренный ролик, а на пустом
 * стенде их нет: прогон сам заводит экран «E2E_стенд_…» и картинку-ролик
 * «E2E_ролик_…» (служебная категория — одобрена без модерации) и в конце
 * убирает ВСЁ, что создал, — и когда проверка упала. Плейлист с показом,
 * созданный по ходу, удаляется страховочной уборкой по названию.
 *
 * Запуск — через ui_test.sh (поднимает контейнер с Chromium).
 * Переменные: DS_URL (по умолч. http://nginx), ADMIN_USER, ADMIN_PASS.
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
  const fx = { screenId: null, mediaId: null };

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
    // повторы ×N (предупреждение «подряд») → «Создать и отправить» → все
    // экраны сети, завтра, 10–11 ч → «Отправить». Дальше — карточка плейлиста
    // и «По датам» в Расписании, потом отмена показа и удаление плейлиста.
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
      await page.evaluate(() =>
        document.querySelector('input[data-action="plc-target-all"]').click());
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
      (/Все экраны сети/.test(rowText) && /10:00 – 11:00/.test(rowText))
        ? ok('в строке «Все экраны сети», 10:00 – 11:00')
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
    if (fx.screenId || fx.mediaId) {
      head('Уборка тестовых объектов');
      step = 'уборка';
      dialogMode = 'dismiss';
      try {
        await closeModals(page);
        // Плейлист ищем по названию: цикл мог упасть до удаления. Показы уходят вместе с ним.
        const pls = await apiCall(page, 'GET', '/playlists');
        for (const p of (pls.data || []).filter(p => p.name === PLAYLIST_NAME)) {
          const r = await apiCall(page, 'DELETE', '/playlists/' + p.id);
          r.status === 200 ? ok('недоудалённый тестовый плейлист убран (вместе с показами)')
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
