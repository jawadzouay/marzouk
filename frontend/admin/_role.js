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

  // Big floating pill that takes the manager to their own agent inbox.
  // Mirror of _manager_toggle.js (which lives on the agent side and
  // takes them back here). Two-way switch — role itself never changes.
  function injectAgentViewToggle() {
    if (document.getElementById('mgrToggleBtn')) return;
    var btn = document.createElement('button');
    btn.id = 'mgrToggleBtn';
    btn.type = 'button';
    btn.innerHTML = '📥 الانتقال إلى صفحة الوكيل';
    btn.title = 'عرض رسائلك الشخصية كوكيل';
    btn.onclick = function () { window.location.href = '/agent/leads.html'; };
    btn.style.cssText = [
      'position:fixed',
      'top:max(10px, env(safe-area-inset-top))',
      'left:12px',
      'z-index:99997',
      'background:linear-gradient(135deg,#10b981,#059669)',
      'color:white',
      'border:2px solid #047857',
      'border-radius:14px',
      'padding:9px 14px',
      'font-family:Cairo,Tajawal,sans-serif',
      'font-size:12.5px',
      'font-weight:900',
      'cursor:pointer',
      'box-shadow:0 6px 18px rgba(5,150,105,0.45)',
      'transition:transform .12s ease, box-shadow .12s ease',
    ].join(';');
    btn.onmouseenter = function(){ btn.style.transform = 'translateY(-1px)'; btn.style.boxShadow = '0 8px 22px rgba(5,150,105,0.55)'; };
    btn.onmouseleave = function(){ btn.style.transform = ''; btn.style.boxShadow = '0 6px 18px rgba(5,150,105,0.45)'; };
    document.body.appendChild(btn);
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
