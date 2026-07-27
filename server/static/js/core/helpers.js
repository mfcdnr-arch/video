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
  return isNaN(d) ? String(ts) : d.toLocaleString('ru-RU');
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
  }[r] || r || '—';
}
