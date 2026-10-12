// Feed: likes, comments, carousels, the composer and infinite scroll.
// Clicks are handled on the document, so posts loaded later work the same way.
(function () {
  'use strict';
  var toastBox = document.getElementById('feed-toast');
  var toastTimer;

  function toast(text) {
    toastBox.textContent = text;
    toastBox.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastBox.hidden = true; }, 2600);
  }

  function send(form, extra) {
    var data = new FormData(form);
    if (extra) Object.keys(extra).forEach(function (k) { data.append(k, extra[k]); });
    return fetch(form.action, { method: 'POST', body: data, headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin' })
      .then(function (r) {
        return r.json().catch(function () { return { error: 'Something went wrong (error ' + r.status + ').' }; });
      });
  }

  function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  // ---- likes ---------------------------------------------------------------
  function likesText(names) {
    if (!names.length) return '';
    var others = names.length - 1;
    return 'Liked by <strong>' + escapeHtml(names[0]) + '</strong>' +
      (others > 0 ? ' and <strong>' + others + ' other' + (others > 1 ? 's' : '') + '</strong>' : '');
  }

  function like(form, onlyAdd) {
    var button = form.querySelector('button');
    if (onlyAdd && button.classList.contains('is-liked')) return;
    button.classList.toggle('is-liked');  // Show it straight away; the server confirms.
    send(form).then(function (data) {
      if (data.error) { button.classList.toggle('is-liked'); toast(data.error); return; }
      button.classList.toggle('is-liked', data.liked);
      button.setAttribute('aria-pressed', data.liked ? 'true' : 'false');
      if (data.liked) { button.classList.remove('pop'); void button.offsetWidth; button.classList.add('pop'); }
      var post = form.closest('.feed-post');
      var line = post.querySelector('.feed-likes');
      line.querySelector('[data-likes-text]').innerHTML = likesText(data.likers);
      line.hidden = !data.count;
    }).catch(function () { button.classList.toggle('is-liked'); toast('No connection. Try again.'); });
  }

  // ---- sharing -------------------------------------------------------------
  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    var box = document.createElement('textarea');
    box.value = text;
    box.setAttribute('readonly', '');
    box.style.position = 'fixed';
    box.style.opacity = '0';
    document.body.appendChild(box);
    box.select();
    var ok = document.execCommand('copy');
    box.remove();
    return ok ? Promise.resolve() : Promise.reject();
  }

  function share(url) {
    if (navigator.share) {
      navigator.share({ url: url }).catch(function () {});
      return;
    }
    copy(url).then(function () { toast('Link copied'); }, function () { window.prompt('Copy this link:', url); });
  }

  // ---- carousels -----------------------------------------------------------
  function updateCarousel(carousel) {
    var slides = carousel.querySelector('.feed-slides');
    var index = Math.round(slides.scrollLeft / Math.max(1, slides.clientWidth));
    var count = slides.children.length;
    var dots = carousel.querySelectorAll('.feed-dots span');
    dots.forEach(function (dot, i) { dot.classList.toggle('is-on', i === index); });
    var counter = carousel.querySelector('.feed-slide-count');
    if (counter) counter.textContent = (index + 1) + '/' + count;
    var prev = carousel.querySelector('.is-prev');
    var next = carousel.querySelector('.is-next');
    if (prev) prev.hidden = index === 0;
    if (next) next.hidden = index >= count - 1;
    // Pause a video that has slid out of view.
    Array.prototype.forEach.call(slides.querySelectorAll('video'), function (video) {
      if (video.closest('.feed-slide') !== slides.children[index]) video.pause();
    });
  }

  function setUp(root) {
    root.querySelectorAll('.feed-carousel').forEach(function (carousel) {
      if (carousel.dataset.ready) return;
      carousel.dataset.ready = '1';
      var slides = carousel.querySelector('.feed-slides');
      var timer;
      slides.addEventListener('scroll', function () {
        clearTimeout(timer);
        timer = setTimeout(function () { updateCarousel(carousel); }, 60);
      }, { passive: true });
    });
    // Long posts start folded, with "See more".
    root.querySelectorAll('.feed-more-text').forEach(function (button) {
      if (button.dataset.ready) return;
      button.dataset.ready = '1';
      var body = button.previousElementSibling;
      body.classList.add('is-clamped');
      if (body.scrollHeight > body.clientHeight + 4) button.hidden = false;
      else body.classList.remove('is-clamped');
    });
  }
  setUp(document);

  // ---- double tap to like, tap to view full size ---------------------------
  var lightbox = document.getElementById('feed-lightbox');
  function openLightbox(src) {
    lightbox.querySelector('img').src = src;
    lightbox.hidden = false;
    document.body.classList.add('is-locked');
  }
  function closeLightbox() {
    lightbox.hidden = true;
    lightbox.querySelector('img').removeAttribute('src');
    document.body.classList.remove('is-locked');
  }
  lightbox.addEventListener('click', closeLightbox);
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && !lightbox.hidden) closeLightbox();
  });

  var lastTap = { target: null, time: 0, timer: null };
  document.addEventListener('click', function (event) {
    var img = event.target.closest('.feed-slide img');
    if (!img) return;
    var now = Date.now();
    if (lastTap.target === img && now - lastTap.time < 320) {
      clearTimeout(lastTap.timer);
      lastTap.target = null;
      var post = img.closest('.feed-post');
      var heart = post.querySelector('.feed-heart');
      heart.classList.remove('is-on'); void heart.offsetWidth; heart.classList.add('is-on');
      like(post.querySelector('.feed-like-form'), true);
      return;
    }
    lastTap = { target: img, time: now, timer: setTimeout(function () { openLightbox(img.dataset.full || img.src); }, 330) };
  });

  // ---- clicks ----------------------------------------------------------------
  document.addEventListener('click', function (event) {
    var target = event.target;
    var el;

    // Close an open "…" menu when clicking anywhere else.
    document.querySelectorAll('.feed-menu[open]').forEach(function (menu) {
      if (!menu.contains(target)) menu.open = false;
    });

    if ((el = target.closest('[data-share]'))) {
      event.preventDefault();
      var menu = el.closest('.feed-menu');
      if (menu) menu.open = false;
      share(el.dataset.share);
    } else if ((el = target.closest('[data-focus-comment]'))) {
      var box = el.closest('.feed-post').querySelector('.feed-comment-form textarea');
      box.focus();
    } else if ((el = target.closest('[data-reply]'))) {
      var reply = el.closest('.feed-post').querySelector('.feed-comment-form textarea');
      var mention = '@' + el.dataset.reply + ' ';
      if (reply.value.indexOf(mention) === -1) reply.value = mention + reply.value;
      reply.focus();
      reply.setSelectionRange(reply.value.length, reply.value.length);
    } else if ((el = target.closest('.feed-all-comments'))) {
      el.nextElementSibling.classList.remove('is-collapsed');
      el.remove();
    } else if ((el = target.closest('.feed-more-text'))) {
      el.previousElementSibling.classList.remove('is-clamped');
      el.remove();
    } else if ((el = target.closest('.feed-slide-nav'))) {
      var slides = el.closest('.feed-carousel').querySelector('.feed-slides');
      slides.scrollBy({ left: (el.classList.contains('is-next') ? 1 : -1) * slides.clientWidth, behavior: 'smooth' });
    } else if ((el = target.closest('[data-edit]'))) {
      var post = el.closest('.feed-post');
      el.closest('.feed-menu').open = false;
      post.querySelector('[data-body]').hidden = true;
      var editForm = post.querySelector('.feed-edit');
      editForm.hidden = false;
      var area = editForm.querySelector('textarea');
      area.focus();
      area.setSelectionRange(area.value.length, area.value.length);
    } else if ((el = target.closest('[data-edit-cancel]'))) {
      var editing = el.closest('.feed-post');
      editing.querySelector('.feed-edit').hidden = true;
      editing.querySelector('[data-body]').hidden = false;
    } else if ((el = target.closest('[data-view]'))) {
      var grid = el.dataset.view === 'grid';
      document.getElementById('feed-grid').hidden = !grid;
      document.getElementById('feed-list').hidden = grid;
      document.querySelectorAll('[data-view]').forEach(function (b) { b.setAttribute('aria-selected', String(b === el)); });
    }
  });

  // ---- forms -----------------------------------------------------------------
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    if (form.classList.contains('feed-like-form')) {
      event.preventDefault();
      like(form);
    } else if (form.classList.contains('feed-comment-form')) {
      event.preventDefault();
      var box = form.querySelector('textarea');
      if (!box.value.trim() || form.dataset.busy) return;
      form.dataset.busy = '1';
      send(form).then(function (data) {
        delete form.dataset.busy;
        if (data.error) { toast(data.error); return; }
        var list = form.closest('.feed-post').querySelector('.feed-comments');
        list.insertAdjacentHTML('beforeend', data.html);
        relativeTimes(list);
        box.value = '';
        box.style.height = '';
      }).catch(function () { delete form.dataset.busy; toast('No connection. Try again.'); });
    } else if (form.classList.contains('feed-edit')) {
      event.preventDefault();
      send(form).then(function (data) {
        if (data.error) { toast(data.error); return; }
        var post = form.closest('.feed-post');
        var body = post.querySelector('[data-body]');
        body.innerHTML = data.html;
        body.classList.toggle('is-empty', !data.html);
        body.hidden = false;
        form.hidden = true;
        toast('Post updated');
      }).catch(function () { toast('No connection. Try again.'); });
    }
  });

  // Enter sends a comment; Shift+Enter makes a new line. Text boxes grow as you type.
  document.addEventListener('keydown', function (event) {
    var box = event.target;
    if (!box.matches || !box.matches('.feed-comment-form textarea')) return;
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      box.form.requestSubmit ? box.form.requestSubmit() : box.form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
    }
  });
  document.addEventListener('input', function (event) {
    var box = event.target;
    if (box.tagName !== 'TEXTAREA' || !box.closest('.feed-page-area, .feed-main')) return;
    box.style.height = 'auto';
    box.style.height = Math.min(box.scrollHeight + 2, 320) + 'px';
  });

  // Only one video plays at a time.
  document.addEventListener('play', function (event) {
    document.querySelectorAll('.feed-main video').forEach(function (video) {
      if (video !== event.target) video.pause();
    });
  }, true);

  function relativeTimes(root) {
    // notify.js formats times every minute; nudge it for new content.
    root.querySelectorAll('time[data-relative]').forEach(function (el) { el.textContent = 'just now'; });
  }

  // ---- infinite scroll -------------------------------------------------------
  var loading = document.getElementById('feed-loading');
  function watchNext() {
    var sentinel = document.querySelector('.feed-next[data-next]:not([data-next=""])');
    if (!sentinel || !('IntersectionObserver' in window)) return;
    var observer = new IntersectionObserver(function (entries) {
      if (!entries[0].isIntersecting) return;
      observer.disconnect();
      loading.hidden = false;
      fetch(sentinel.dataset.next, { credentials: 'same-origin' })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          var holder = document.createElement('div');
          holder.innerHTML = html;
          var list = sentinel.parentNode;
          while (holder.firstChild) list.insertBefore(holder.firstChild, sentinel);
          sentinel.remove();
          setUp(list);
          loading.hidden = true;
          watchNext();
        })
        .catch(function () { loading.textContent = 'Could not load more posts. Scroll again to retry.'; observer.observe(sentinel); });
    }, { rootMargin: '600px' });
    observer.observe(sentinel);
  }
  watchNext();

  // ---- composer --------------------------------------------------------------
  var composer = document.getElementById('feed-composer');
  if (!composer) return;
  var bodyBox = document.getElementById('feed-body');
  var input = document.getElementById('feed-media');
  var previews = document.getElementById('feed-previews');
  var hint = document.getElementById('feed-hint');
  var maxFiles = 10;
  var files = [];

  function isVideo(file) { return /^video\//.test(file.type) || /\.(mp4|m4v|mov|webm)$/i.test(file.name); }

  function render() {
    previews.innerHTML = '';
    files.forEach(function (item, index) {
      var li = document.createElement('li');
      var canShow = isVideo(item.file) || /^image\/(jpeg|png|gif|webp)$/.test(item.file.type);
      if (canShow) {
        var el = document.createElement(isVideo(item.file) ? 'video' : 'img');
        el.src = item.url;
        if (el.tagName === 'VIDEO') { el.muted = true; li.classList.add('is-video'); } else el.alt = '';
        li.appendChild(el);
      } else {
        li.className = 'is-name';
        li.textContent = item.file.name;
      }
      var remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'feed-preview-remove';
      remove.setAttribute('aria-label', 'Remove ' + item.file.name);
      remove.textContent = '✕';
      remove.addEventListener('click', function () {
        URL.revokeObjectURL(item.url);
        files.splice(index, 1);
        render();
      });
      li.appendChild(remove);
      previews.appendChild(li);
    });
    previews.hidden = !files.length;
    hint.textContent = files.length ? files.length + ' of ' + maxFiles + ' added' : hint.dataset.idle;
    if (files.length) composer.classList.add('is-open');
  }
  hint.dataset.idle = hint.textContent;

  function add(list) {
    var skipped = 0;
    Array.prototype.forEach.call(list, function (file) {
      if (!/^(image|video)\//.test(file.type) && !/\.(heic|heif|mov|m4v)$/i.test(file.name)) { skipped++; return; }
      if (files.length >= maxFiles) { skipped++; return; }
      files.push({ file: file, url: URL.createObjectURL(file) });
    });
    if (skipped) toast(files.length >= maxFiles ? 'Up to ' + maxFiles + ' photos and videos per post' : 'Only photos and videos can be added');
    render();
  }

  input.addEventListener('change', function () { add(input.files); input.value = ''; });
  bodyBox.addEventListener('focus', function () { composer.classList.add('is-open'); });
  bodyBox.addEventListener('paste', function (event) {
    var pasted = event.clipboardData && event.clipboardData.files;
    if (pasted && pasted.length) { event.preventDefault(); add(pasted); }
  });

  // Drag photos onto the composer (PC).
  var dropHint = document.getElementById('feed-drop-hint');
  var depth = 0;
  composer.addEventListener('dragenter', function (event) { event.preventDefault(); depth++; dropHint.hidden = false; composer.classList.add('is-dragging'); });
  composer.addEventListener('dragover', function (event) { event.preventDefault(); });
  composer.addEventListener('dragleave', function () { if (--depth <= 0) { depth = 0; dropHint.hidden = true; composer.classList.remove('is-dragging'); } });
  composer.addEventListener('drop', function (event) {
    event.preventDefault();
    depth = 0;
    dropHint.hidden = true;
    composer.classList.remove('is-dragging');
    add(event.dataTransfer.files);
  });

  // The round + button on phones jumps to the composer.
  var fab = document.getElementById('feed-fab');
  if (fab) {
    fab.addEventListener('click', function () {
      composer.scrollIntoView({ behavior: 'smooth', block: 'start' });
      setTimeout(function () { bodyBox.focus(); }, 300);
    });
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (entries) { fab.classList.toggle('is-shown', !entries[0].isIntersecting); })
        .observe(composer);
    }
  }

  // Upload with a progress bar, since videos can be large.
  composer.addEventListener('submit', function (event) {
    event.preventDefault();
    if (!bodyBox.value.trim() && !files.length) { toast('Write something or add a photo or video'); bodyBox.focus(); return; }
    var button = document.getElementById('feed-submit');
    var progress = document.getElementById('feed-progress');
    var fill = document.getElementById('feed-progress-fill');
    var text = document.getElementById('feed-progress-text');
    var data = new FormData();
    data.append('_csrf_token', composer.querySelector('[name=_csrf_token]').value);
    data.append('body', bodyBox.value);
    files.forEach(function (item) { data.append('media', item.file, item.file.name); });
    var xhr = new XMLHttpRequest();
    xhr.open('POST', composer.action);
    xhr.setRequestHeader('X-Requested-With', 'fetch');
    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      var pct = Math.round(e.loaded / e.total * 100);
      fill.style.width = pct + '%';
      text.textContent = pct < 100 ? 'Uploading… ' + pct + '%' : 'Checking the files…';
    };
    function failed(message) {
      button.disabled = false;
      button.textContent = 'Post';
      progress.hidden = true;
      toast(message);
    }
    xhr.onload = function () {
      var result = {};
      try { result = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status === 200 && result.url) { window.location.assign(result.url.split('#')[0]); return; }
      failed(result.error || 'The post could not be sent (error ' + xhr.status + ').');
    };
    xhr.onerror = function () { failed('The connection was lost while uploading. Try again.'); };
    button.disabled = true;
    button.textContent = 'Posting…';
    fill.style.width = '0';
    text.textContent = 'Uploading…';
    progress.hidden = !files.length;
    xhr.send(data);
  });
})();
