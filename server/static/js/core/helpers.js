// Общие helper-функции UI

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
