//=============================================================================
// КАРКАС
//=============================================================================
const NAVS = [
  ['dash','Дашборд','▤'],['broadcast','Эфир сети','⇄'],['ticker','Бегущая строка','▦'],
  ['screens','Экраны','▣'],['media','Медиатека','◫'],['playlists','Плейлисты','▶'],['schedule','Расписание','▦'],
  ['campaigns','Кампании','📣'],['moderation','Модерация','☑'],['reports','Отчёты','◳'],
  ['advertisers','Рекламодатели','◍'],['billing','Биллинг','₽'],['ota','Обновление агентов','⟳'],['settings','Настройки','⚙']
];

let CUR = 'dash';

function initLayoutViewActions(){
  if(window.__layoutViewActionsInitialized) return;
  window.__layoutViewActionsInitialized = true;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('layout-')) return;

    switch(action){
      case 'layout-nav': {
        const v = el.dataset.nav;
        return Signage.nav(v);
      }

      case 'layout-logout':
        return Signage.logout();
    }
  });
}

// Рекламодателю доступен ровно один раздел — его кабинет. Это только внешний
// вид: сервер режет доступ независимо от меню (deps.advertiser_scope и
// middleware в main.py), потому что скрытая ссылка защитой не является.
function navsForRole(){
  return ME.role === 'advertiser'
    ? [['advertisers', 'Мой кабинет', '◍']]
    : NAVS;
}

function renderApp(){
  document.getElementById('app').innerHTML = `
    <nav>
      <div class="brand">◉ DS Admin</div>
      ${navsForRole().map(n => `<a class="nav" data-v="${n[0]}" data-action="layout-nav" data-nav="${n[0]}"><span class="ic">${n[2]}</span><span class="lbl">${n[1]}</span></a>`).join('')}

      <div class="sep"></div>
      <a class="nav" data-action="layout-logout"><span class="ic">⎋</span><span class="lbl">Выйти</span></a>
    </nav>
    <main>
      <div class="top"><div class="title" id="vtitle">Дашборд</div><div class="top-right" id="topright"></div></div>
      <div class="body" id="view"><div class="empty">Загрузка…</div></div>
    </main>`;
}

function nav(v){
  CUR = v;
  document.querySelectorAll('.nav').forEach(n => n.classList.toggle('on', n.dataset.v === v));

  const t = {
    dash:'Дашборд',
    broadcast:'Эфир сети',
    ticker:'Бегущая строка',
    screens:'Экраны',
    media:'Медиатека',
    playlists:'Плейлисты',
    schedule:'Расписание',
    campaigns:'Кампании',
    moderation:'Модерация',
    reports:'Отчёты',
    advertisers:'Рекламодатели',
    billing:'Биллинг',
    ota:'Обновление агентов',
    settings:'Настройки'
  }[v];

  document.getElementById('vtitle').textContent =
    (v === 'advertisers' && ME.role === 'advertiser') ? 'Мой кабинет' : t;
  document.getElementById('topright').innerHTML = '';

  ({
    dash:viewDash,
    broadcast:viewBroadcast,
    ticker:viewTicker,
    screens:viewScreens,
    media:viewMedia,
    playlists:viewPlaylists,
    schedule:viewSchedule,
    campaigns:viewCampaigns,
    moderation:viewModeration,
    reports:viewReports,
    advertisers:viewAdvertisers,
    billing:viewBilling,
    ota:viewOta,
    settings:viewSettings
  }[v])();

  setTimeout(applyRoleGuard, 60);
}

function applyRoleGuard(){
  const otaNav = document.querySelector('.nav[data-v="ota"]');
  if(otaNav) otaNav.style.display = canWrite() ? '' : 'none';
  if(canWrite()) return;

  document.querySelectorAll('#view .btn.primary, #view .btn.danger, #app .top-right .btn.primary, .iconbtn').forEach(b => {
    if(b.closest('#mypass')) return;
    b.style.display = 'none';
  });
}

window.Signage = window.Signage || {};
window.Signage.nav = nav;

initLayoutViewActions();

