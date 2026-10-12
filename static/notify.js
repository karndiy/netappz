// The bell in the top bar: keeps the unread count fresh and pops up new notifications.
(function () {
  var bell = document.getElementById('topbar-bell');
  if (!bell || bell.dataset.ready) return;
  bell.dataset.ready = '1';
  var badge = bell.querySelector('.topbar-badge');
  var latest = parseInt(bell.dataset.latest, 10) || 0;
  var baseTitle = document.title.replace(/^\(\d+\+?\) /, '');

  function show(count) {
    badge.textContent = count > 99 ? '99+' : String(count);
    badge.hidden = !count;
    bell.setAttribute('aria-label', 'Notifications' + (count ? ', ' + count + ' unread' : ''));
    document.title = (count ? '(' + (count > 99 ? '99+' : count) + ') ' : '') + baseTitle;
  }

  var stack;
  function toast(item) {
    if (!stack) {
      stack = document.createElement('div');
      stack.className = 'alert-toasts';
      stack.setAttribute('role', 'status');
      stack.setAttribute('aria-live', 'polite');
      document.body.appendChild(stack);
    }
    var link = document.createElement('a');
    link.className = 'alert-toast';
    link.href = item.url;
    link.textContent = item.message;
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'alert-toast-close';
    close.setAttribute('aria-label', 'Close');
    close.textContent = '✕';
    close.addEventListener('click', function (event) { event.preventDefault(); link.remove(); });
    link.appendChild(close);
    stack.appendChild(link);
    setTimeout(function () { link.remove(); }, 9000);
  }

  function poll() {
    if (document.hidden) return;
    fetch(bell.dataset.poll + '?after=' + latest, { headers: { Accept: 'application/json' }, credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        show(data.unread);
        if (bell.dataset.popups !== '0') data.fresh.slice().reverse().forEach(toast);
        latest = Math.max(latest, data.latest || 0);
      })
      .catch(function () {});
  }

  show(parseInt(badge.textContent, 10) || 0);
  setInterval(poll, 20000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });

  // "5 min ago" style times on the Feed and Notifications pages.
  function relative() {
    var now = Date.now();
    document.querySelectorAll('time[data-relative]').forEach(function (el) {
      var when = new Date(el.getAttribute('datetime'));
      if (isNaN(when)) return;
      var s = Math.max(0, (now - when) / 1000);
      var text;
      if (s < 60) text = 'just now';
      else if (s < 3600) text = Math.floor(s / 60) + ' min ago';
      else if (s < 86400) text = Math.floor(s / 3600) + ' h ago';
      else if (s < 7 * 86400) text = Math.floor(s / 86400) + ' d ago';
      else text = when.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
      el.textContent = text;
      el.title = when.toLocaleString();
    });
  }
  relative();
  setInterval(relative, 60000);
})();
