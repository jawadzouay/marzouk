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

  // Inject the "switch to agent view" pill INTO the page header — that's
  // the dark sticky bar at the top of every admin page, so the pill is
  // guaranteed visible regardless of subnav layout, scroll position, or
  // body stacking contexts. Falls back to a body-level fixed pill for
  // pages that don't have a <header>.
  function injectAgentViewToggle() {
    if (document.getElementById('mgrToggleBtn')) return;
    var btn = document.createElement('button');
    btn.id = 'mgrToggleBtn';
    btn.type = 'button';
    btn.innerHTML = '📥 صفحة الوكيل';
    btn.title = 'عرض رسائلك الشخصية كوكيل';
    btn.onclick = function () { window.location.href = '/agent/leads.html'; };
    btn.style.background    = 'linear-gradient(135deg, #10b981, #059669)';
    btn.style.color         = 'white';
    btn.style.border        = '2px solid #047857';
    btn.style.borderRadius  = '10px';
    btn.style.padding       = '7px 14px';
    btn.style.fontFamily    = "'Cairo', 'Tajawal', sans-serif";
    btn.style.fontSize      = '12.5px';
    btn.style.fontWeight    = '900';
    btn.style.cursor        = 'pointer';
    btn.style.boxShadow     = '0 4px 12px rgba(5,150,105,0.5)';
    btn.style.whiteSpace    = 'nowrap';
    btn.style.marginInlineEnd = '8px';

    var header = document.querySelector('header');
    if (header) {
      // Stick it before the first action button in the header so it sits
      // inside the visible dark bar. Different pages use different anchor
      // classes (.logout-btn / .back-btn / .header-right > button), so try
      // a list of probable anchors before falling back to appendChild.
      var anchor = header.querySelector('.logout-btn')
                || header.querySelector('.back-btn')
                || header.querySelector('button')
                || header.querySelector('a.action-btn, a.back-btn');
      if (anchor && anchor.parentNode) {
        anchor.parentNode.insertBefore(btn, anchor);
      } else {
        header.appendChild(btn);
      }
    } else {
      // Fallback: floating in the corner.
      btn.style.position = 'fixed';
      btn.style.top = '10px';
      btn.style.left = '10px';
      btn.style.zIndex = '99997';
      document.body.appendChild(btn);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      hideBlocked(); injectScopeBadge(); injectAgentViewToggle();
    });
  } else {
    hideBlocked();
    injectScopeBadge();
    injectAgentViewToggle();
  }
})();
