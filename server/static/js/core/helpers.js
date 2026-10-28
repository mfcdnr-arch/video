// Общие helper-функции UI

// Серверные метки времени (created_at и т.п.) приходят БЕЗ часового пояса,
// но фактически это UTC (Postgres в контейнере). new Date('...') без Z
// трактует их как местные и показывает со сдвигом (для МСК — на 3 часа
// назад). Добавляем Z, если пояс не указан, и показываем в местном времени.
function parseServerTS(ts) {
  const s = String(ts);
  const hasTZ = /Z$|[+-]\d\d:?\d\d$/.test(s);
  return new Date(hasTZ ? s : s + 'Z');
}

function fmtServerTS(ts) {
  if (!ts) return '—';
  const d = parseServerTS(ts);
  if (isNaN(d)) return String(ts);
  // с днём недели: «пн, 28.07.2026, 09:15:00»
  return d.toLocaleDateString('ru-RU', {weekday: 'short'}) + ', ' +
         d.toLocaleString('ru-RU');
}

// «В сети» решает сервер (поле online у экрана: последний сигнал не старше 5 минут),
// а подпись «не в сети …» строится по offline_for_seconds — секундам с последнего сигнала.
// null (экран ни разу не выходил на связь) → «ни разу не выходил на связь».
function fmtOfflineFor(seconds) {
  if (seconds === null || seconds === undefined || seconds === '') return 'ни разу не выходил на связь';
  const sec = Number(seconds);
  if (!isFinite(sec)) return 'ни разу не выходил на связь';
  if (sec < 3600) return 'не в сети ' + Math.floor(Math.max(sec, 0) / 60) + ' мин';
  if (sec < 86400) {
    return 'не в сети ' + Math.floor(sec / 3600) + ' ч ' + Math.floor((sec % 3600) / 60) + ' мин';
  }
  return 'не в сети ' + Math.floor(sec / 86400) + ' дн';
}

function kpi(l, v, s, col) {
  return `<div class="kpi"><div class="l">${l}</div><div class="v"${col?` style="color:${col}"`:''}>${v}</div>${s?`<div class="s"${col?` style="color:${col}"`:''}>${s}</div>`:''}</div>`;
}

function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

function roleLabel(r) {
  return {
    superadmin: 'Супер-админ',
    admin: 'Админ (все права)',
    auditor: 'Аудитор (просмотр и отчёты)',
    moderator: 'Модератор (проверка рекламы, 38-ФЗ)',
    advertiser: 'Рекламодатель (только свой кабинет)',
  }[r] || r || '—';
}
