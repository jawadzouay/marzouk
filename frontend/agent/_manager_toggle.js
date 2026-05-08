// Manager view-switcher — for users who logged in with role='manager'.
// Lets them flip between the admin dashboard (their full team view) and
// the agent dashboard (their own personal lead pile). The role itself
// doesn't change — only the page they land on. Both views are accessible
// by them server-side (require_agent now allows manager too).
//
// Loaded on every agent page. Mirror script lives inside _role.js for
// the admin side. Both inject a big, obvious yellow pill so it can't
// be missed.
(function () {
  var role = localStorage.getItem('role');
  if (role !== 'manager') return;

  function inject() {
    if (document.getElementById('mgrToggleBtn')) return;
    var btn = document.createElement('button');
    btn.id = 'mgrToggleBtn';
    btn.type = 'button';
    btn.innerHTML = '🎖 العودة إلى لوحة الإدارة';
    btn.title = 'الرجوع إلى عرض المدير الكامل';
    btn.onclick = function () { window.location.href = '/admin/dashboard.html'; };
    btn.style.cssText = [
      'position:fixed',
      'top:max(10px, env(safe-area-inset-top))',
      'left:12px',                             // RTL — visually top-right corner
      'z-index:99997',
      'background:linear-gradient(135deg,#fbbf24,#f59e0b)',
      'color:#78350f',
      'border:2px solid #d97706',
      'border-radius:14px',
      'padding:9px 14px',
      'font-family:Cairo,Tajawal,sans-serif',
      'font-size:12.5px',
      'font-weight:900',
      'cursor:pointer',
      'box-shadow:0 6px 18px rgba(245,158,11,0.45)',
      'transition:transform .12s ease, box-shadow .12s ease',
    ].join(';');
    btn.onmouseenter = function(){ btn.style.transform = 'translateY(-1px)'; btn.style.boxShadow = '0 8px 22px rgba(245,158,11,0.55)'; };
    btn.onmouseleave = function(){ btn.style.transform = ''; btn.style.boxShadow = '0 6px 18px rgba(245,158,11,0.45)'; };
    document.body.appendChild(btn);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }
})();
