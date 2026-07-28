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
 *   4. Прогоняет два цикла с уборкой за собой: экран и плейлист
 *      (создать → увидеть в списке → удалить).
 *   5. Копит ошибки консоли, необработанные исключения и ответы HTTP ≥400.
 *
 * Любая ошибка консоли/JS и любой ответ ≥400 — это провал прогона: именно
 * так выглядели дефекты, которые ловили руками.
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

// Разделы левого меню: [data-nav, заголовок в шапке].
const SECTIONS = [
  ['dash', 'Дашборд'], ['broadcast', 'Эфир сети'], ['ticker', 'Бегущая строка'],
  ['screens', 'Экраны'], ['media', 'Медиатека'], ['playlists', 'Плейлисты'],
  ['schedule', 'Расписание'], ['campaigns', 'Кампании'], ['moderation', 'Модерация'],
  ['reports', 'Отчёты'], ['billing', 'Биллинг'], ['ota', 'Обновление агентов'],
  ['settings', 'Настройки'],
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
  'schedule-mode-week', 'schedule-mode-month', 'schedule-change-month',
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

function record(kind, text) {
  if (IGNORE_TEXT.some(re => re.test(text))) return;
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

    // ── 5. Плейлист: создать → найти → удалить ─────────────────────────────
    head('Цикл «плейлист»: создание и удаление');
    step = 'создание плейлиста';
    await gotoSection(page, 'playlists');
    dialogMode = 'accept';
    promptAnswer = PLAYLIST_NAME;
    const createBtn = await page.$('[data-action="playlists-create"]');
    if (!createBtn) {
      bad('кнопка «+ Плейлист» не найдена');
    } else {
      await createBtn.click();
      await sleep(1200);
      await gotoSection(page, 'playlists');
      const has = await page.evaluate(n => document.getElementById('view').innerText.includes(n), PLAYLIST_NAME);
      has ? ok('плейлист создан и виден в списке') : bad('плейлист не появился в списке');

      step = 'удаление плейлиста';
      const el = await page.$(
        `[data-action="playlists-delete"][data-playlist-name="${PLAYLIST_NAME}"]`);
      if (!el) {
        bad('кнопка удаления тестового плейлиста не найдена');
      } else {
        await el.click();
        await sleep(1200);
        await gotoSection(page, 'playlists');
        const still = await page.evaluate(n => document.getElementById('view').innerText.includes(n), PLAYLIST_NAME);
        still ? bad('плейлист остался в списке после удаления') : ok('плейлист удалён, список обновился');
      }
    }

    // ── 6. Индикатор монитора (V2, миграция 028) ───────────────────────────
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
