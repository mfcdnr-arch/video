//=============================================================================
// ПРОСМОТР РОЛИКА
//
// Клик по миниатюре в медиатеке (data-action="mprev-open" data-media-id="…")
// открывает окно: слева кадр (видео с перемоткой или картинка), справа сведения
// — длительность, размер кадра, файл, папка/рекламодатель, статус модерации,
// число плейлистов. На узком экране сведения уходят под кадр.
//
// Signage.openMediaPreview(mediaId) вызывается и из других окон (сборка
// плейлиста и т.п.): просмотр открывается поверх них, закрытие возвращает
// к окну под ним. Файлы раздаёт nginx по /media/<имя> (с Range — перемотка)
// только по входу: <video>/<img> несут cookie ds_media (см. mediaSession).
//=============================================================================

const MPREV_IMG_RE = /\.(png|jpe?g|bmp|webp|gif)$/i;
const MPREV_STATUS = {
  approved: ['одобрен', 'var(--accent)'],
  pending:  ['на модерации', '#ffd34d'],
  rejected: ['отклонён', 'var(--danger)'],
};
const MPREV_BUSY = new Set();   // id роликов, сведения которых сейчас грузятся (защита от двойного клика)

function mprevDuration(sec){
  sec = Math.round(Number(sec) || 0);
  if(sec <= 0) return '—';
  return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0');
}

function mprevSize(bytes){
  const b = Number(bytes) || 0;
  if(b <= 0) return '—';
  const mb = b / 1048576;
  const NB = '\u00a0';   // число и единица не разрываются переносом строки
  if(mb >= 1024) return (mb / 1024).toFixed(1).replace('.', ',') + NB + 'ГБ';
  if(mb >= 10) return mb.toFixed(0) + NB + 'МБ';
  if(mb >= 0.1) return mb.toFixed(1).replace('.', ',') + NB + 'МБ';
  return 'меньше' + NB + '0,1' + NB + 'МБ';
}

// api() бросает Error(тело ответа); у FastAPI это {"detail": "..."} — достаём текст.
function mprevErrText(e){
  const t = String((e && e.message) || e || '');
  try{ const j = JSON.parse(t); if(j && typeof j.detail === 'string') return j.detail; }catch(_){}
  return t;
}

async function openMediaPreview(mediaId){
  const id = Number(mediaId);
  if(!id || MPREV_BUSY.has(id)) return null;
  MPREV_BUSY.add(id);
  let m;
  try{
    // cookie файлов — заодно: без неё nginx не отдаст кадр (401)
    [m] = await Promise.all([api('/media/' + id), mediaSession()]);
  }catch(e){
    toast('Не удалось открыть ролик: ' + mprevErrText(e));
    return null;
  }finally{
    MPREV_BUSY.delete(id);
  }
  if(m.status === 'document'){ toast('Документ нельзя просмотреть — только скачать'); return null; }

  const title = m.title || m.filename || ('Ролик ' + id);
  const src = location.origin + '/media/' + encodeURIComponent(m.filename || '');
  const isImg = MPREV_IMG_RE.test(m.filename || '');

  const media = isImg
    ? `<img src="${esc(src)}" alt="${esc(title)}">`
    : `<video controls autoplay playsinline preload="metadata" autofocus src="${esc(src)}"></video>`;

  const st = MPREV_STATUS[m.review_status];
  const rows = [
    [isImg ? 'Длительность показа' : 'Длительность', `<b>${mprevDuration(m.duration_seconds)}</b>`],
    ['Размер кадра', `<b data-mprev="dim">—</b>`],
    ['Файл', `<b>${esc(m.filename || '—')}</b> · ${mprevSize(m.filesize)}`],
  ];
  if(m.advertiser_name) rows.push(['Рекламодатель', `<b>${esc(m.advertiser_name)}</b>`]);
  rows.push(['Папка', `<b>${m.folder_name ? esc(m.folder_name) : 'без папки'}</b>`]);
  rows.push(['Статус', st ? `<b style="color:${st[1]};">${st[0]}</b>` : `<b>${esc(m.review_status || '—')}</b>`]);
  if(m.review_status === 'rejected' && m.reject_reason) rows.push(['Причина отклонения', `<b>${esc(m.reject_reason)}</b>`]);
  rows.push(['В плейлистах', `<b>${Number(m.playlists_count) || 0}</b>`]);

  const body = `<div class="mprev">
      <div class="mprev-media">${media}</div>
      <div class="mprev-info">${rows.map(r => `<div>${r[0]}: ${r[1]}</div>`).join('')}</div>
    </div>`;

  const modal = openModal({
    title, body, width: 860, className: 'modal-mprev',
    onClose(){
      // Рвём загрузку: без этого браузер ещё держит Range-соединение с nginx.
      const v = modal && modal.el.querySelector('video');
      if(v){ v.pause(); v.removeAttribute('src'); v.load(); }
    },
  });

  const box = modal.el.querySelector('.mprev-media');
  const dim = modal.el.querySelector('[data-mprev="dim"]');
  const el = box.firstElementChild;
  const setDim = (w, h) => { if(w > 0 && h > 0) dim.textContent = w + '×' + h; };
  const fail = () => {
    box.innerHTML = '<div class="mprev-err">Не удалось загрузить файл. Возможно, он повреждён или в неподдерживаемом формате — его можно скачать из медиатеки.</div>';
  };

  if(isImg){
    if(el.complete && el.naturalWidth) setDim(el.naturalWidth, el.naturalHeight);
    el.addEventListener('load', () => setDim(el.naturalWidth, el.naturalHeight));
    el.addEventListener('error', fail);
    modal.el.focus();
  }else{
    el.addEventListener('loadedmetadata', () => setDim(el.videoWidth, el.videoHeight));
    el.addEventListener('error', fail);
    if(el.readyState >= 1) setDim(el.videoWidth, el.videoHeight);
  }
  return modal;
}

// Клик (и Enter/Пробел на фокусе) по миниатюре — открыть просмотр.
function initMediaPreviewActions(){
  if(window.__mediaPreviewActionsInitialized) return;
  window.__mediaPreviewActionsInitialized = true;

  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('mprev-')) return;

    switch(action){
      case 'mprev-open': {
        // миниатюра лежит внутри кликабельной карточки — карточка не должна сработать
        e.stopPropagation();
        return Signage.openMediaPreview(Number(el.dataset.mediaId));
      }
    }
  });

  document.addEventListener('keydown', e => {
    if(e.key !== 'Enter' && e.key !== ' ') return;
    const el = e.target.closest && e.target.closest('[data-action="mprev-open"]');
    if(!el || el !== e.target) return;   // только когда фокус на самой миниатюре
    e.preventDefault();
    e.stopPropagation();
    Signage.openMediaPreview(Number(el.dataset.mediaId));
  });
}

window.Signage = window.Signage || {};
window.Signage.openMediaPreview = openMediaPreview;

initMediaPreviewActions();
