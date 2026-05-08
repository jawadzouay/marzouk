// Role-aware bootstrap for every admin page.
// - Hides subnav links marked [data-admin-only] when the signed-in user
//   is a branch manager.
// - Redirects managers away from admin-only pages (body[data-admin-only-page]).
// - Adds a scope badge into the subnav.
//
// Pages should:
//   1. Mark their forbidden subnav links with `data-admin-only`.
//   2. Add `data-admin-only-page` to <body> if the WHOLE page is admin-only.
//   3. Allow managers in their auth gate (`role === 'admin' || role === 'manager'`).
(function () {
  var role = localStorage.getItem('role');
  if (!role) return;

  // Admin-only page guard — managers get bounced.
  if (role === 'manager' && document.body && document.body.dataset.adminOnlyPage === 'true') {
    alert('هذه الصفحة للمدير العام فقط');
    location.href = '/admin/dashboard.html';
    return;
  }

  if (role !== 'manager') return;

  document.body && document.body.classList.add('role-manager');

  // Hide admin-only elements — works for subnav links, page sections,
  // form rows, buttons, anything with [data-admin-only].
  function hideBlocked() {
    document.querySelectorAll('[data-admin-only]').forEach(function (el) {
      el.style.display = 'none';
    });
  }
  function injectScopeBadge() {
    var st = localStorage.getItem('scope_type') || '';
    var label =
      st === 'all'   ? '🎖 مدير عام (بدون إعلانات / إنفاق)' :
      st === 'city'  ? '🎖 مدير مدينة' :
      st === 'branch'? '🎖 مدير فرع' :
                       '🎖 مدير';
    var nav = document.querySelector('.subnav');
    if (!nav || nav.querySelector('.scope-badge')) return;
    var badge = document.createElement('span');
    badge.className = 'scope-badge';
    badge.style.cssText =
      'background:#fef3c7;color:#92400e;padding:4px 10px;border-radius:12px;' +
      'font-size:11px;font-weight:800;margin-right:auto;align-self:center;' +
      'white-space:nowrap;';
    badge.textContent = label;
    nav.appendChild(badge);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { hideBlocked(); injectScopeBadge(); });
  } else {
    hideBlocked();
    injectScopeBadge();
  }
})();
