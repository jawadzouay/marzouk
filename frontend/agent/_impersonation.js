// Read-only impersonation banner.
//
// Admin clicks 👀 عرض كوكيل on the admin leads page → backend mints an
// agent JWT with impersonated_by=<admin_sub>; the admin leads page
// stashes the admin's original token in localStorage.admin_token_backup
// and redirects here. This script:
//   1. Detects the backup token (= we're in impersonation mode)
//   2. Pins a red banner at the top of the page with the agent's name
//      and an exit button
//   3. Disables every form / button / link that would trigger a write
//      so the admin can't accidentally update anything
//   4. Suppresses the phone-gate + post-call modals (they're irrelevant
//      and would trap the admin)
//
// Backend independently rejects every write with 403 — this layer is
// purely UX so the admin doesn't try to interact with disabled controls.
(function () {
  var backup = null;
  try { backup = localStorage.getItem('admin_token_backup') || null; } catch (e) {}
  if (!backup) return;  // not impersonating

  // Tag the body so the rest of the app + other scripts can react.
  document.body && document.body.classList.add('impersonating');
  // Mark a flag the other scripts can read — phone-gate + post-call
  // bail out when they see this.
  window.__MZ_IMPERSONATING = true;

  var agentName = '';
  try { agentName = localStorage.getItem('impersonating_agent_name') || ''; } catch (e) {}

  function exitImpersonation() {
    try {
      var t  = localStorage.getItem('admin_token_backup') || '';
      var r  = localStorage.getItem('admin_role_backup')  || 'admin';
      var n  = localStorage.getItem('admin_name_backup')  || '';
      if (t) localStorage.setItem('token', t);
      localStorage.setItem('role', r);
      if (n) {
        localStorage.setItem('name', n);
        localStorage.setItem('agentName', n);
      } else {
        localStorage.removeItem('agentName');
      }
      localStorage.removeItem('admin_token_backup');
      localStorage.removeItem('admin_role_backup');
      localStorage.removeItem('admin_name_backup');
      localStorage.removeItem('impersonating_agent_name');
    } catch (e) {}
    window.location.href = '/admin/leads.html';
  }
  window.MzExitImpersonation = exitImpersonation;

  // Inject CSS that disables interactive write controls + styles the
  // banner. Pointer-events:none on writes so they don't fire; opacity
  // so the agent can see the page is in read mode.
  var STYLES = ''
    + '.mz-imp-banner {'
    + '  position: fixed; top: 0; left: 0; right: 0; z-index: 99996;'
    + '  background: linear-gradient(135deg, #dc2626, #b91c1c); color: white;'
    + '  padding: 9px 14px; padding-top: max(9px, env(safe-area-inset-top));'
    + '  display: flex; align-items: center; justify-content: space-between;'
    + '  font-family: "Cairo","Tajawal",sans-serif; font-size: 12.5px;'
    + '  font-weight: 800; box-shadow: 0 4px 14px rgba(220,38,38,0.4);'
    + '  direction: rtl; gap: 10px;'
    + '}'
    + '.mz-imp-banner b { font-weight: 900; }'
    + '.mz-imp-banner .mz-imp-exit {'
    + '  background: white; color: #b91c1c; border: 0;'
    + '  padding: 6px 14px; border-radius: 9px; cursor: pointer;'
    + '  font-family: inherit; font-size: 12px; font-weight: 900;'
    + '  white-space: nowrap;'
    + '}'
    + '.mz-imp-banner .mz-imp-exit:hover { background: #fef2f2; }'
    + 'body.impersonating { padding-top: 44px !important; }'
    + 'body.impersonating header { top: 44px !important; }'  /* sticky header sits below banner */

    /* Disable everything that would write. Selectors match the writeable
       UI surfaces we know about: status dropdowns, call/WA links, the
       primary action buttons in modals (save/submit/...). Navigation,
       filter chips, view-only buttons stay clickable. */
    + 'body.impersonating .status-select,'
    + 'body.impersonating .call,'
    + 'body.impersonating .wa,'
    + 'body.impersonating .agent-call-btn,'
    + 'body.impersonating .call-icon-btn,'
    + 'body.impersonating .modal-save,'
    + 'body.impersonating .pco-btn,'
    + 'body.impersonating .pco-rdv-save,'
    + 'body.impersonating .pco-custom-save,'
    + 'body.impersonating .modal-save,'
    + 'body.impersonating .credentials-form button[type=submit],'
    + 'body.impersonating .submit-btn,'
    + 'body.impersonating .goal-save,'
    + 'body.impersonating button[onclick*="saveCredentials"],'
    + 'body.impersonating button[onclick*="saveGoal"],'
    + 'body.impersonating button[onclick*="updateStatus"],'
    + 'body.impersonating input[type=file],'
    + 'body.impersonating textarea {'
    + '  pointer-events: none !important;'
    + '  opacity: 0.55 !important;'
    + '  cursor: not-allowed !important;'
    + '  filter: grayscale(0.4);'
    + '}'
    /* Form inputs are still keyboard-editable (admin might want to type
       to test), but their submit buttons are blocked above. */
    ;

  function inject() {
    var style = document.createElement('style');
    style.textContent = STYLES;
    document.head.appendChild(style);

    var bar = document.createElement('div');
    bar.className = 'mz-imp-banner';
    bar.innerHTML =
      '<span>👀 وضع المعاينة — تتصفّح كـ <b>' + escapeHtml(agentName) + '</b> (قراءة فقط)</span>'
      + '<button class="mz-imp-exit" type="button">✕ خروج للوحة الإدارة</button>';
    bar.querySelector('.mz-imp-exit').addEventListener('click', exitImpersonation);
    document.body.insertBefore(bar, document.body.firstChild);
  }

  function escapeHtml(s) {
    return (s || '').toString()
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }
})();
