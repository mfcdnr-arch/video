//=============================================================================
// МОДАЛЬНЫЕ ОКНА (стопкой)
//
//   openModal({title, body, footer, width, className, onClose}) -> {el, close}
//     title     — обычный ТЕКСТ (экранируется внутри, esc() снаружи не нужен);
//     body      — готовый HTML содержимого;
//     footer    — готовый HTML подвала (кнопки); пусто — подвала нет;
//     width     — максимальная ширина окна в px (по умолчанию 560; на узком
//                 экране окно не шире 92vw);
//     className — дополнительный класс на .modal;
//     onClose   — вызывается один раз при закрытии (любым способом).
//     el — элемент .modal (внутри .modal-head, .modal-body, .modal-foot).
//     Любой элемент внутри окна с атрибутом data-modal-close закрывает его.
//
//   closeTopModal() — закрыть верхнее окно (true, если оно было);
//   closeAllModals() — закрыть все.
//
// Окна складываются стопкой: новое открывается поверх предыдущего, Esc и клик
// по затемнённому фону закрывают ТОЛЬКО верхнее, после чего фокус возвращается
// туда, откуда окно открыли. Пока окно открыто, Tab ходит только внутри него.
//=============================================================================
(function(){
  const Z0 = 1000;          // z-index нижнего окна; каждое следующее +2
  const stack = [];         // открытые окна, последнее — верхнее
  let seq = 0;

  const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]):not([type="hidden"]),'
    + 'select:not([disabled]),textarea:not([disabled]),video[controls],audio[controls],'
    + 'summary,[contenteditable="true"],[tabindex]:not([tabindex="-1"])';

  function focusables(root){
    return Array.from(root.querySelectorAll(FOCUSABLE))
      .filter(el => !el.hasAttribute('data-modal-sentinel') && el.getClientRects().length > 0);
  }

  // Фокус внутрь окна: элемент с autofocus, иначе само окно.
  function focusInto(modal){
    const af = modal.querySelector('[autofocus]');
    (af || modal).focus({preventScroll: true});
  }

  function openModal(opts){
    opts = opts || {};
    const id = ++seq;
    const opener = document.activeElement;
    const width = Number(opts.width) > 0 ? Number(opts.width) : 560;

    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    backdrop.style.zIndex = String(Z0 + stack.length * 2);
    backdrop.innerHTML =
      '<div tabindex="0" data-modal-sentinel="start"></div>'
      + `<div class="modal${opts.className ? ' ' + esc(opts.className) : ''}" role="dialog" aria-modal="true"
             aria-labelledby="modal-title-${id}" tabindex="-1" style="width:min(${width}px,92vw);">
          <div class="modal-head">
            <h3 class="modal-title" id="modal-title-${id}">${esc(opts.title)}</h3>
            <button type="button" class="modal-x" aria-label="Закрыть" data-modal-close>✕</button>
          </div>
          <div class="modal-body">${opts.body || ''}</div>
          ${opts.footer ? `<div class="modal-foot">${opts.footer}</div>` : ''}
        </div>`
      + '<div tabindex="0" data-modal-sentinel="end"></div>';
    const modal = backdrop.querySelector('.modal');
    const [sentStart, sentEnd] = backdrop.querySelectorAll('[data-modal-sentinel]');

    const entry = {backdrop, modal, opener, close};

    // Закрытие по фону — только если и нажатие, и отпускание были на фоне
    // (выделение текста мышью с выходом за край окна его не закрывает).
    let downOnBackdrop = true;
    backdrop.addEventListener('mousedown', e => { downOnBackdrop = (e.target === backdrop); });
    backdrop.addEventListener('click', e => {
      const onBackdrop = (e.target === backdrop) && downOnBackdrop;
      downOnBackdrop = true;
      if(onBackdrop) return close();
      const x = e.target.closest && e.target.closest('[data-modal-close]');
      if(x && modal.contains(x)) close();
    });

    // Края цикла Tab: с первого элемента Shift+Tab уходит в конец, с последнего Tab — в начало.
    sentStart.addEventListener('focus', () => {
      const f = focusables(modal);
      (f[f.length - 1] || modal).focus();
    });
    sentEnd.addEventListener('focus', () => {
      const f = focusables(modal);
      (f[0] || modal).focus();
    });

    // Нижнее окно «замирает»: ни кликов, ни фокуса, пока над ним есть другое.
    const below = stack[stack.length - 1];
    if(below) below.backdrop.setAttribute('inert', '');
    stack.push(entry);
    document.body.appendChild(backdrop);
    document.body.classList.add('modal-open');
    focusInto(modal);

    function close(){
      const i = stack.indexOf(entry);
      if(i < 0) return;                       // уже закрыто
      const wasTop = (i === stack.length - 1);
      stack.splice(i, 1);
      backdrop.remove();
      const top = stack[stack.length - 1];
      if(top && wasTop) top.backdrop.removeAttribute('inert');
      if(!top) document.body.classList.remove('modal-open');
      try{ if(typeof opts.onClose === 'function') opts.onClose(); }
      catch(err){ console.error('modal onClose:', err); }
      if(!wasTop) return;                     // закрыли нижнее из кода — фокус не трогаем
      // Фокус — туда, откуда открывали (если это место ещё на странице и доступно).
      const back = entry.opener;
      if(back && back !== document.body && back.isConnected && typeof back.focus === 'function'
         && (!top || top.modal.contains(back))){
        back.focus({preventScroll: true});
      }else if(top){
        focusInto(top.modal);
      }
    }

    return {el: modal, close};
  }

  function closeTopModal(){
    const top = stack[stack.length - 1];
    if(!top) return false;
    top.close();
    return true;
  }

  function closeAllModals(){
    while(stack.length) stack[stack.length - 1].close();
  }

  // Esc закрывает верхнее окно. Если внутри окна кто-то уже обработал Esc
  // (preventDefault) — не вмешиваемся.
  document.addEventListener('keydown', e => {
    if(e.key !== 'Escape' || e.defaultPrevented || e.isComposing) return;
    if(closeTopModal()) e.preventDefault();
  });

  // Фокус не должен уходить из верхнего окна на страницу под ним.
  document.addEventListener('focusin', e => {
    const top = stack[stack.length - 1];
    if(!top || top.backdrop.contains(e.target)) return;
    focusInto(top.modal);
  });

  window.openModal = openModal;
  window.closeTopModal = closeTopModal;
  window.closeAllModals = closeAllModals;
  window.Signage = window.Signage || {};
  window.Signage.openModal = openModal;
  window.Signage.closeTopModal = closeTopModal;
  window.Signage.closeAllModals = closeAllModals;
})();
