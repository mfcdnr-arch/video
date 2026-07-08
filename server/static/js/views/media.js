//=============================================================================
// МЕДИАТЕКА
//=============================================================================

let MEDIA_ADV = null;

// Инициализация действий для медиа-вида
function initMediaViewActions(){
  if(window.__mediaViewActionsInitialized) return;
  window.__mediaViewActionsInitialized = true;

  // Клики по кнопкам и ссылкам
  document.addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(!action || !action.startsWith('media-')) return;

    switch(action){
      case 'media-create-adv':
        return createAdvPrompt();

      case 'media-open-upload': {
        const advId = el.dataset.advId ? Number(el.dataset.advId) : undefined;
        return uploadForm(advId);
      }

      case 'media-save-adv-price': {
        e.stopPropagation();
        const advId = Number(el.dataset.advId);
        return saveAdvPrice(advId);
      }

      case 'media-open-adv': {
        const advId = Number(el.dataset.advId);
        const advName = el.dataset.advName || '';
        return openAdv(advId, advName);
      }

      case 'media-back':
        return nav('media');

      case 'media-delete-adv': {
        const advId = Number(el.dataset.advId);
        const advName = el.dataset.advName || '';
        return deleteAdv(advId, advName);
      }

      case 'media-delete-folder': {
        const folderId  = Number(el.dataset.folderId);
        const folderName = el.dataset.folderName || '';
        const advId     = Number(el.dataset.advId);
        const advName   = el.dataset.advName || '';
        return deleteFolder(folderId, folderName, advId, advName);
      }

      case 'media-create-folder': {
        const advId   = Number(el.dataset.advId);
        const advName = el.dataset.advName || '';
        return createFolderInAdv(advId, advName);
      }

      case 'media-download': {
        const fileId = Number(el.dataset.fileId);
        return dlMedia(fileId);
      }

      case 'media-banner-duration': {
        const fileId = Number(el.dataset.fileId);
        const cur = el.dataset.seconds || '10';
        return setBannerDuration(fileId, cur);
      }

      case 'media-toggle-filler': {
        const fileId   = Number(el.dataset.fileId);
        const isFiller = el.dataset.isFiller === 'true';
        const nextValue = !isFiller;
        return toggleFiller(fileId, nextValue);
      }

      case 'media-delete-file': {
        const fileId   = Number(el.dataset.fileId);
        const fileName = el.dataset.fileName || '';
        return delMedia(fileId, fileName);
      }

      case 'media-create-adv-in-form':
        return createAdvInForm();

      case 'media-submit-upload':
        return doUpload();
    }
  });

  // Изменение полей формы загрузки
  document.addEventListener('change', e => {
    const el = e.target.closest('[data-action]');
    if(!el) return;

    const action = el.dataset.action;
    if(action === 'media-upload-adv-change'){
      return loadFoldersInForm();
    }
    if(action === 'media-upload-cat-change'){
      return applyAdCategoryRules();
    }
  });

  // Поиск по медиатеке (debounce 300мс)
  let searchTimer = null;
  document.addEventListener('input', e => {
    const el = e.target.closest('[data-action="media-search-input"]');
    if(!el) return;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => runMediaSearch(el.value.trim()), 300);
  });
}

async function runMediaSearch(q){
  const box = document.getElementById('media-search-results');
  const advList = document.getElementById('media-adv-list');
  if(!box) return;
  if(q.length < 2){
    box.innerHTML = '';
    if(advList) advList.style.display = '';
    return;
  }
  if(advList) advList.style.display = 'none';
  try{
    const rows = await api('/media/search?q=' + encodeURIComponent(q));
    if(!rows.length){ box.innerHTML = '<div class="empty">Ничего не найдено</div>'; return; }
    const ST = {pending:'<span style="color:#ffd34d;">на модерации</span>',
                rejected:'<span style="color:var(--danger);">отклонён</span>',
                approved:''};
    let h = `<div class="muted" style="font-size:11px;margin-bottom:7px;">Найдено: ${rows.length}</div>`;
    rows.forEach(m => {
      h += `<div class="cell" style="display:flex;align-items:center;gap:10px;margin-bottom:6px;${m.advertiser_id?'cursor:pointer;':''}"
        ${m.advertiser_id?`data-action="media-open-adv" data-adv-id="${m.advertiser_id}" data-adv-name="${esc(m.advertiser||'')}"`:''}>
        <div style="width:64px;height:36px;flex-shrink:0;background:var(--bg2);border-radius:5px;overflow:hidden;position:relative;">
          <img src="${API}/media/${m.id}/thumbnail" alt="" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover;" onerror="this.remove()">
        </div>
        <div style="flex:1;min-width:0;">
          <div style="font-size:12px;font-weight:500;">${esc(m.title||m.filename)} ${m.is_filler?'<span class="muted" style="font-size:10px;">(заглушка)</span>':''}</div>
          <div class="muted" style="font-size:11px;">${esc(m.advertiser||'без рекламодателя')}${m.duration_seconds?' · '+Math.round(m.duration_seconds)+' c':''}${m.age_rating?' · '+esc(m.age_rating):''}</div>
        </div>
        <div style="font-size:11px;">${ST[m.review_status]||''}</div>
        <span class="dim">${m.advertiser_id?'›':''}</span>
      </div>`;
    });
    box.innerHTML = h;
  }catch(e){ box.innerHTML = '<div class="empty">Ошибка: ' + esc(e.message) + '</div>'; }
}

// Основной вид медиатеки
async function viewMedia(){
  MEDIA_ADV = null;

  // Верхние кнопки
  document.getElementById('topright').innerHTML = canWrite()
    ? `
      <button class="btn" data-action="media-create-adv" style="margin-right:6px;">+ Рекламодатель</button>
      <button class="btn primary" data-action="media-open-upload">↑ Загрузить файл</button>`
    : '';

  const view = document.getElementById('view');

  try{
    const advs = await api('/advertisers');
    advs.forEach(a => ADV_COLORS[a.id] = a.color);

    if(!advs.length){
      view.innerHTML = '<div class="empty">Рекламодателей пока нет.<br>Нажмите «+ Рекламодатель» чтобы создать, или «↑ Загрузить файл» — при загрузке тоже можно создать рекламодателя.</div>';
      return;
    }

    let h = `<div class="fld" style="margin-bottom:10px;">
      <input class="inp" id="media-search" placeholder="🔍 Поиск: название ролика, файл, рекламодатель…" data-action="media-search-input" autocomplete="off">
    </div>
    <div id="media-search-results"></div>
    <div id="media-adv-list">`;
    h += '<div class="muted" style="font-size:12px;margin-bottom:11px;">Рекламодатель → папки → файлы. Цена за минуту эфира используется для расчёта заработка в отчётах.</div>';

    advs.forEach(a => {
      const priceCtl = canWrite()
        ? `<span style="display:inline-flex;align-items:center;gap:4px;">
             <input class="inp" id="price-${a.id}" value="${a.price_per_minute||0}" style="width:80px;padding:5px 8px;text-align:right;" title="Цена за минуту эфира">
             <span class="muted" style="font-size:11px;">₽/мин</span>
             <button class="btn"
               style="padding:5px 10px;font-size:12px;"
               data-action="media-save-adv-price"
               data-adv-id="${a.id}">💾</button>
           </span>`
        : `<span class="muted" style="font-size:12px;">${a.price_per_minute||0} ₽/мин</span>`;

      h += `<div class="cell" style="display:flex;align-items:center;gap:10px;cursor:pointer;margin-bottom:7px;"
               data-action="media-open-adv"
               data-adv-id="${a.id}"
               data-adv-name="${esc(a.name)}">
               <span class="dot" style="background:${a.color};"></span>
               <span style="font-weight:500;width:140px;">${esc(a.name)}</span>
               <span style="color:var(--muted);font-size:12px;">${a.folders} папок · ${a.files} файлов</span>
               <span style="flex:1;"></span>
               ${priceCtl}<span class="dim">›</span>
             </div>`;
    });
    h += '</div>';

    view.innerHTML = h;
  }catch(e){
    view.innerHTML = '<div class="empty">Ошибка: ' + esc(e.message) + '</div>';
  }
}

async function createAdvPrompt(){
  const name = prompt('Название рекламодателя:');
  if(!name || !name.trim()) return;
  try{
    await api('/advertisers', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name.trim()})});
    toast('Рекламодатель создан'); viewMedia();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function saveAdvPrice(id){
  const v = parseFloat(val('price-' + id));
  if(isNaN(v) || v < 0){ toast('Введите корректную цену'); return; }
  try{
    await api('/advertisers/' + id + '/price', {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({price_per_minute:v})});
    toast('Цена сохранена');
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function openAdv(id, name){
  MEDIA_ADV = {id, name};
  document.getElementById('topright').innerHTML = '';
  const view = document.getElementById('view');
  try{
    const [folders, files] = await Promise.all([api('/advertisers/' + id + '/folders'), api('/advertisers/' + id + '/media')]);

    let h = `<div style="display:flex;align-items:center;gap:9px;margin-bottom:13px;">
      <button class="btn" data-action="media-back">← Назад</button>
      <span class="dot" style="background:${ADV_COLORS[id]||'var(--accent)'};"></span>
      <span style="font-weight:500;">${esc(name)}</span>
      ${canWrite()?`<button class="btn danger" style="margin-left:auto;margin-right:4px;" data-action="media-delete-adv" data-adv-id="${id}" data-adv-name="${esc(name)}">🗑 Удалить</button>`:'<span style="flex:1;"></span>'}
      <button class="btn primary" data-action="media-open-upload" data-adv-id="${id}">↑ Загрузить файл</button></div>`;

    // Папки с управлением
    let foldersHtml = `<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px;">
      <span class="muted" style="font-size:12px;">Папки:</span>`;
    if(folders.length){
      folders.forEach(f => {
        foldersHtml += `<span style="display:inline-flex;align-items:center;gap:4px;background:var(--bg2);border-radius:6px;padding:3px 8px;font-size:12px;">
          ${esc(f.name)} <span class="muted">(${f.files})</span>
          ${canWrite()?`<button class="iconbtn del" style="padding:0 2px;font-size:11px;" title="Удалить папку" data-action="media-delete-folder" data-folder-id="${f.id}" data-folder-name="${esc(f.name)}" data-adv-id="${id}" data-adv-name="${esc(name)}">✕</button>`:''}
        </span>`;
      });
    } else { foldersHtml += '<span class="muted" style="font-size:12px;">нет</span>'; }
    if(canWrite()) foldersHtml += `<button class="btn" style="padding:3px 10px;font-size:12px;" data-action="media-create-folder" data-adv-id="${id}" data-adv-name="${esc(name)}">+ Папка</button>`;
    foldersHtml += '</div>';
    h += foldersHtml;

    if(!files.length){ h += '<div class="empty">В этой медиатеке пока нет файлов</div>'; view.innerHTML = h; return; }

    h += '<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr));">';
    files.forEach(f => {
      h += `<div class="cell" style="padding:0;overflow:hidden;">
        <div style="height:88px;background:var(--bg2);display:flex;align-items:center;justify-content:center;color:#454b57;font-size:26px;position:relative;">
          <img src="${API}/media/${f.id}/thumbnail" alt=""
               style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover;"
               onerror="this.remove()">▶
          ${f.in_playlist?'<span style="position:absolute;top:6px;left:6px;background:#0f6e56;color:#dffaf0;font-size:10px;padding:1px 7px;border-radius:5px;z-index:1;">в эфире</span>':''}
          ${f.review_status==='pending'?'<span style="position:absolute;top:6px;right:6px;background:#8a6d00;color:#ffe9a8;font-size:10px;padding:1px 7px;border-radius:5px;z-index:1;">на модерации</span>':''}
          ${f.review_status==='rejected'?`<span title="${esc(f.reject_reason||'')}" style="position:absolute;top:6px;right:6px;background:#7a1f1f;color:#ffd7d7;font-size:10px;padding:1px 7px;border-radius:5px;z-index:1;">отклонён</span>`:''}
          ${f.age_rating?`<span style="position:absolute;bottom:6px;right:6px;background:rgba(0,0,0,.65);color:#fff;font-size:10px;padding:1px 6px;border-radius:5px;z-index:1;">${esc(f.age_rating)}</span>`:''}</div>
        <div style="padding:9px 10px;">
          <div style="font-size:12px;font-weight:500;word-break:break-all;">${esc(f.title||f.filename)}</div>
          <div style="font-size:11px;color:var(--muted);margin:2px 0 7px;">${fmtSize(f.filesize)}${f.duration_seconds?' · '+Math.round(f.duration_seconds)+' c':''}</div>
          <div style="display:flex;gap:5px;border-top:0.5px solid var(--border2);padding-top:7px;">
            <button class="iconbtn" title="Скачать" data-action="media-download" data-file-id="${f.id}">⤓</button>
            ${/\.(png|jpe?g|bmp|webp|gif)$/i.test(f.filename||'')?`<button class="iconbtn" title="Длительность показа баннера" data-action="media-banner-duration" data-file-id="${f.id}" data-seconds="${Math.round(f.duration_seconds||10)}">⏱</button>`:''}
            <button class="iconbtn" title="${f.is_filler?'Снять пометку заглушки':'Пометить как заглушку'}" data-action="media-toggle-filler" data-file-id="${f.id}" data-is-filler="${f.is_filler?'true':'false'}">${f.is_filler?'🟢':'⬚'}</button>
            <button class="iconbtn del" title="Удалить" style="margin-left:auto;" data-action="media-delete-file" data-file-id="${f.id}" data-file-name="${esc(f.title||f.filename)}">🗑</button>
          </div></div></div>`;
    });
    view.innerHTML = h + '</div>';
  }catch(e){ view.innerHTML = '<div class="empty">Ошибка: ' + esc(e.message) + '</div>'; }
}

async function createFolderInAdv(advId, advName){
  const name = prompt('Название папки:');
  if(!name || !name.trim()) return;
  try{
    await api('/advertisers/' + advId + '/folders', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name.trim()})});
    toast('Папка создана'); openAdv(advId, advName);
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function deleteFolder(folderId, folderName, advId, advName){
  if(!confirm('Удалить папку «' + folderName + '»? Ролики останутся.')) return;
  try{
    await api('/folders/' + folderId, {method:'DELETE'});
    toast('Папка удалена'); openAdv(advId, advName);
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function deleteAdv(id, name){
  if(!confirm('Удалить рекламодателя «' + name + '»? Ролики останутся без рекламодателя.')) return;
  try{
    await api('/advertisers/' + id, {method:'DELETE'});
    toast('Рекламодатель удалён'); nav('media');
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function uploadForm(advId){
  document.getElementById('topright').innerHTML = '';
  let advs = []; try{ advs = await api('/advertisers'); }catch(e){}

  const advSel = `<div class="fld"><label>Рекламодатель (необязательно)</label>
      <select class="inp" id="up-adv" data-action="media-upload-adv-change">
        <option value="">— не выбран —</option>
        ${advs.map(a=>`<option value="${a.id}"${advId&&a.id==advId?' selected':''}>${esc(a.name)}</option>`).join('')}
      </select></div>
      <div class="fld" id="up-folder-wrap" style="display:none;">
        <label>Папка (необязательно)</label>
        <select class="inp" id="up-folder"><option value="">— без папки —</option></select>
      </div>
      <div class="fld" style="display:flex;align-items:center;gap:8px;margin-top:4px;">
        <input class="inp" id="up-new-adv" placeholder="Создать нового рекламодателя…" style="flex:1;">
        <button class="btn" data-action="media-create-adv-in-form">+ Создать</button>
      </div>`;

  document.getElementById('view').innerHTML = `
    <div style="max-width:460px;">
      <div class="fld"><label>Видеофайл (MP4) или баннер (JPG / PNG)</label><input class="inp" id="up-file" type="file" accept="video/mp4,image/png,image/jpeg"></div>
      <div class="muted" style="font-size:11px;margin:-6px 0 10px;">Файл автоматически проверяется на работоспособность.</div>
      <div class="fld"><label>Длительность баннера, сек (только для картинок)</label><input class="inp" id="up-imgsec" type="number" min="1" max="300" value="10" style="width:100px;"></div>
      <div class="fld"><label>Название ролика</label><input class="inp" id="up-title" placeholder="Coca-Cola — Лето 2026"></div>
      <div class="sec" style="font-size:12px;margin:14px 0 6px;">Закон о рекламе (38-ФЗ)</div>
      <div class="fld"><label>Категория товара/услуги</label>
        <select class="inp" id="up-cat" data-action="media-upload-cat-change"><option value="">Загрузка…</option></select></div>
      <div class="fld" id="up-age-wrap"><label>Возрастная маркировка</label>
        <select class="inp" id="up-age">
          <option value="">— выберите —</option>
          <option>0+</option><option>6+</option><option>12+</option><option>16+</option><option>18+</option>
        </select></div>
      <div class="fld" id="up-disc-wrap" style="display:none;"><label>Текст обязательного предупреждения</label>
        <input class="inp" id="up-disc" placeholder="">
        <div class="muted" id="up-disc-hint" style="font-size:11px;margin-top:3px;"></div></div>
      <div class="fld" id="up-lic-wrap" style="display:none;"><label>Номер лицензии</label>
        <input class="inp" id="up-lic" placeholder="№ 1234 от 01.01.2020"></div>
      <div class="muted" id="up-cat-note" style="font-size:11px;margin:-4px 0 10px;"></div>
      <div class="sec" style="font-size:12px;margin:14px 0 6px;">Срок действия (необязательно)</div>
      <div class="muted" style="font-size:11px;margin-bottom:8px;">Период, в течение которого ролик разрешён к показу. Пусто = без ограничения.</div>
      <div class="row"><div class="fld" style="flex:1;"><label>Показывать с</label><input class="inp" id="up-from" type="datetime-local"></div>
        <div class="fld" style="flex:1;"><label>Показывать по</label><input class="inp" id="up-until" type="datetime-local"></div></div>
      <div class="sec" style="font-size:12px;margin:14px 0 6px;">Рекламодатель</div>
      ${advSel}
      <div style="display:flex;gap:8px;margin-top:12px;"><button class="btn primary" data-action="media-submit-upload">↑ Загрузить</button>
        <button class="btn" data-action="media-back">Отмена</button></div>
      <div id="up-progress" class="muted" style="font-size:12px;margin-top:12px;"></div>
    </div>`;
  if(advId) loadFoldersInForm();
  loadAdCategories();
}

let AD_CATS = [];
async function loadAdCategories(){
  try{
    if(!AD_CATS.length) AD_CATS = await api('/moderation/categories');
  }catch(e){ AD_CATS = [{key:'other', label:'Прочие товары и услуги'}]; }
  const sel = document.getElementById('up-cat');
  if(!sel) return;
  sel.innerHTML = AD_CATS.map(c => `<option value="${c.key}" ${c.key==='other'?'selected':''}>${esc(c.label)}</option>`).join('');
  applyAdCategoryRules();
}

function applyAdCategoryRules(){
  const key = val('up-cat');
  const cat = AD_CATS.find(c => c.key === key) || {};
  const show = (id, on) => { const el = document.getElementById(id); if(el) el.style.display = on ? '' : 'none'; };
  show('up-age-wrap', !cat.auto_ok);
  show('up-disc-wrap', !!cat.disclaimer);
  show('up-lic-wrap', !!cat.license);
  const hint = document.getElementById('up-disc-hint');
  if(hint) hint.textContent = cat.disclaimer_hint ? 'Например: «' + cat.disclaimer_hint + '»' : '';
  const disc = document.getElementById('up-disc');
  if(disc) disc.placeholder = cat.disclaimer_hint || '';
  const note = document.getElementById('up-cat-note');
  if(note){
    if(cat.blocked) note.innerHTML = '<span style="color:var(--danger);">Реклама этой категории запрещена законом — загрузка будет отклонена.</span>';
    else if(cat.auto_ok) note.textContent = 'Служебный контент — не реклама, модерация не требуется.';
    else if(cat.min_age) note.textContent = 'Маркировка не ниже ' + cat.min_age + '. После загрузки ролик попадёт на модерацию.';
    else note.textContent = 'После загрузки ролик попадёт на модерацию и выйдет в эфир после одобрения.';
  }
}

async function loadFoldersInForm(){
  const advId = document.getElementById('up-adv')?.value;
  const wrap = document.getElementById('up-folder-wrap');
  if(!wrap) return;
  if(!advId){ wrap.style.display = 'none'; return; }
  try{
    const folders = await api('/advertisers/' + advId + '/folders');
    document.getElementById('up-folder').innerHTML =
      '<option value="">— без папки —</option>' + folders.map(f=>`<option value="${f.id}">${esc(f.name)}</option>`).join('');
    wrap.style.display = '';
  }catch(e){ wrap.style.display = 'none'; }
}

async function createAdvInForm(){
  const name = (document.getElementById('up-new-adv')?.value || '').trim();
  if(!name){ toast('Введите название рекламодателя'); return; }
  try{
    await api('/advertisers', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
    toast('Рекламодатель создан'); uploadForm();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function doUpload(){
  const fileEl = document.getElementById('up-file'); const title = val('up-title');
  if(!fileEl.files.length){ toast('Выберите файл'); return; }
  if(!title){ toast('Введите название'); return; }
  const vf = val('up-from'), vu = val('up-until');
  if(vf && vu && vf > vu){ toast('«Показывать по» раньше «Показывать с»'); return; }
  const advId = document.getElementById('up-adv')?.value || '';
  const folderId = document.getElementById('up-folder')?.value || '';
  const fd = new FormData(); fd.append('file', fileEl.files[0]);
  const q = new URLSearchParams({title});
  if(vf) q.append('valid_from', vf); if(vu) q.append('valid_until', vu);
  if(advId) q.append('advertiser_id', advId); if(folderId) q.append('folder_id', folderId);
  const imgSec = parseInt(val('up-imgsec'), 10);
  if(imgSec && imgSec !== 10) q.append('image_seconds', imgSec);
  // 38-ФЗ: декларация категории
  q.append('category', val('up-cat') || 'other');
  if(val('up-age')) q.append('age_rating', val('up-age'));
  if(val('up-disc')) q.append('disclaimer_text', val('up-disc'));
  if(val('up-lic')) q.append('license_number', val('up-lic'));
  document.getElementById('up-progress').textContent = 'Загрузка и проверка файла…';
  try{
    const res = await api('/media/upload?' + q, {method:'POST', body:fd});
    const info = res.duration_seconds?` (${Math.round(res.duration_seconds)} c, ${res.codec||''}${res.resolution?', '+res.resolution:''})`:'';
    if(res.warning){
      alert('Файл загружен, но есть предупреждение:\n\n⚠ ' + res.warning);
    }
    toast('Файл проверен и загружен' + info); nav('media');
  }catch(e){
    document.getElementById('up-progress').innerHTML = '<span style="color:var(--danger);">' + esc(e.message) + '</span>';
  }
}

async function dlMedia(id){ window.open(API + '/files/download/' + id, '_blank'); }

async function setBannerDuration(id, current){
  const v = prompt('Сколько секунд показывать баннер? (1–300)', current);
  if(v === null) return;
  const sec = parseInt(v, 10);
  if(isNaN(sec) || sec < 1 || sec > 300){ toast('Введите число от 1 до 300'); return; }
  try{
    await api('/media/' + id + '/duration', {method:'PATCH',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({seconds: sec})});
    toast('Длительность обновлена: ' + sec + ' с');
    MEDIA_ADV ? openAdv(MEDIA_ADV.id, MEDIA_ADV.name) : viewMedia();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function toggleFiller(id, isFiller){
  isFiller = (isFiller === true || isFiller === 'true');
  try{
    await api('/media/' + id + '/filler', {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({is_filler:isFiller})});
    toast(isFiller?'Помечено как заглушка':'Пометка снята'); MEDIA_ADV?openAdv(MEDIA_ADV.id, MEDIA_ADV.name):viewMedia();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

async function delMedia(id, name){
  if(!confirm('Удалить «' + name + '»?')) return;
  try{
    await api('/media/' + id, {method:'DELETE'});
    toast('Удалено'); MEDIA_ADV?openAdv(MEDIA_ADV.id, MEDIA_ADV.name):viewMedia();
  }catch(e){ toast('Ошибка: ' + e.message); }
}

function fmtSize(b){ if(!b) return '—'; const mb = b/1048576; return mb>1024?(mb/1024).toFixed(1)+' ГБ':mb.toFixed(0)+' МБ'; }

// Экспорт в глобальный объект
window.Signage = window.Signage || {};
window.Signage.viewMedia = viewMedia;

// Инициализация обработчиков
initMediaViewActions();

