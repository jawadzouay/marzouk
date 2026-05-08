// Manager view-switcher for users with role='manager'. Lets them flip
// from the agent inbox back to the admin/manager dashboard.
// Loaded on every agent page; mirrors injectAgentViewToggle() in
// admin/_role.js. Both inject INTO the page <header> (the dark bar at
// the top) so the pill is impossible to miss.
(function () {
  var role = localStorage.getItem('role');
  if (role !== 'manager') return;
  // The "back to admin dashboard" pill is for managers viewing their
  // own agent inbox. During admin impersonation the dedicated red exit
  // banner already provides the way out — don't double up.
  if (localStorage.getItem('admin_token_backup')) return;

  function inject() {
    if (document.getElementById('mgrToggleBtn')) return;
    var btn = document.createElement('button');
    btn.id = 'mgrToggleBtn';
    btn.type = 'button';
    btn.innerHTML = '🎖 لوحة الإدارة';
    btn.title = 'الرجوع إلى عرض المدير الكامل';
    btn.onclick = function () { window.location.href = '/admin/dashboard.html'; };
    btn.style.background    = 'linear-gradient(135deg, #fbbf24, #f59e0b)';
    btn.style.color         = '#78350f';
    btn.style.border        = '2px solid #d97706';
    btn.style.borderRadius  = '10px';
    btn.style.padding       = '7px 14px';
    btn.style.fontFamily    = "'Cairo', 'Tajawal', sans-serif";
    btn.style.fontSize      = '12.5px';
    btn.style.fontWeight    = '900';
    btn.style.cursor        = 'pointer';
    btn.style.boxShadow     = '0 4px 12px rgba(245,158,11,0.5)';
    btn.style.whiteSpace    = 'nowrap';
    btn.style.marginInlineEnd = '8px';

    var header = document.querySelector('header');
    if (header) {
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
      btn.style.position = 'fixed';
      btn.style.top = '10px';
      btn.style.left = '10px';
      btn.style.zIndex = '99997';
      document.body.appendChild(btn);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }
})();
