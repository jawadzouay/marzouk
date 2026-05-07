// Phone-gate — every agent page includes this script. If the logged-in
// agent has no `phone` set, the page is frozen behind a modal that demands
// the work phone number before letting them use the app.
//
// No verification SMS — the warning copy makes it clear the number must be
// correct (admin will use it to call them). On save, the modal closes.
(function () {
  const token = localStorage.getItem('token');
  // Only run for agents (admin / manager skip; admin has no /agents/me row).
  const role = localStorage.getItem('role') || '';
  if (!token || (role && role !== 'agent' && role !== 'manager')) return;

  const API = window.location.origin;

  // ---- Inject CSS once ----
  const css = `
  .phgate-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.78);
    display: flex; align-items: center; justify-content: center;
    z-index: 99999; padding: 20px; backdrop-filter: blur(3px);
    -webkit-backdrop-filter: blur(3px);
    font-family: 'Cairo', 'Tajawal', sans-serif;
  }
  .phgate-box {
    background: white; border-radius: 16px; max-width: 400px; width: 100%;
    padding: 24px 22px; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.4);
    direction: rtl;
  }
  .phgate-title {
    font-size: 18px; font-weight: 800; color: #1a1a1a; margin-bottom: 6px;
    text-align: center;
  }
  .phgate-sub {
    font-size: 13px; color: #6b7280; line-height: 1.7; text-align: center;
    margin-bottom: 16px;
  }
  .phgate-warn {
    background: #fef3c7; border: 1.5px solid #fbbf24; border-radius: 10px;
    padding: 10px 12px; font-size: 12.5px; color: #78350f; line-height: 1.7;
    margin-bottom: 14px; font-weight: 600;
  }
  .phgate-warn b { color: #92400e; }
  .phgate-label { font-size: 12px; font-weight: 700; color: #374151; display: block; margin-bottom: 6px; }
  .phgate-input {
    width: 100%; padding: 12px 14px; border: 1.5px solid #d1d5db;
    border-radius: 10px; font-size: 16px; font-family: inherit;
    direction: ltr; text-align: left; letter-spacing: 1px;
    box-sizing: border-box;
  }
  .phgate-input:focus { outline: none; border-color: #e63329; }
  .phgate-actions { display: flex; flex-direction: column; gap: 8px; margin-top: 14px; }
  .phgate-save {
    background: #e63329; color: white; border: none; padding: 13px;
    border-radius: 10px; font-weight: 800; font-size: 15px; cursor: pointer;
    font-family: inherit;
  }
  .phgate-save:disabled { opacity: 0.6; cursor: wait; }
  .phgate-logout {
    background: transparent; color: #6b7280; border: none; padding: 8px;
    font-weight: 600; font-size: 13px; cursor: pointer; font-family: inherit;
  }
  .phgate-msg { font-size: 12px; text-align: center; margin-top: 10px; font-weight: 600; }
  .phgate-msg.err { color: #dc2626; }
  .phgate-msg.ok  { color: #059669; }
  `;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  function buildModal() {
    const wrap = document.createElement('div');
    wrap.className = 'phgate-overlay';
    wrap.id = 'phgate-overlay';
    wrap.innerHTML = `
      <div class="phgate-box" role="dialog" aria-modal="true">
        <div class="phgate-title">📞 رقم هاتفك للعمل</div>
        <div class="phgate-sub">
          المرجو إدخال رقم هاتفك الذي تستخدمه للعمل حتى نتمكّن من التواصل معك. <b>تأكّد من صحة الرقم.</b>
        </div>
        <label class="phgate-label" for="phgate-input">رقم الهاتف</label>
        <input id="phgate-input" type="tel" class="phgate-input"
               placeholder="06XXXXXXXX" inputmode="numeric"
               autocomplete="tel" maxlength="14" />
        <div class="phgate-actions">
          <button class="phgate-save" id="phgate-save">حفظ ومتابعة</button>
          <button class="phgate-logout" onclick="(function(){localStorage.clear();location.href='/index.html';})()">تسجيل الخروج</button>
        </div>
        <div class="phgate-msg" id="phgate-msg"></div>
      </div>
    `;
    document.body.appendChild(wrap);
    document.body.style.overflow = 'hidden';

    const input = document.getElementById('phgate-input');
    const btn   = document.getElementById('phgate-save');
    const msg   = document.getElementById('phgate-msg');
    setTimeout(() => input.focus(), 50);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') btn.click(); });

    btn.onclick = async () => {
      const phone = input.value.trim();
      if (!phone) {
        msg.className = 'phgate-msg err';
        msg.textContent = 'أدخل رقم الهاتف';
        return;
      }
      btn.disabled = true;
      msg.className = 'phgate-msg';
      msg.textContent = 'جاري الحفظ...';
      try {
        const res = await fetch(API + '/agents/me/phone', {
          method: 'PATCH',
          headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
          body: JSON.stringify({ phone })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          msg.className = 'phgate-msg err';
          msg.textContent = data.detail || 'فشل الحفظ — تحقّق من الرقم';
          btn.disabled = false;
          return;
        }
        msg.className = 'phgate-msg ok';
        msg.textContent = '✅ تم الحفظ';
        setTimeout(() => {
          wrap.remove();
          document.body.style.overflow = '';
        }, 600);
      } catch (e) {
        msg.className = 'phgate-msg err';
        msg.textContent = 'خطأ في الاتصال';
        btn.disabled = false;
      }
    };
  }

  // Run on every load — fetch /agents/me, gate if phone missing.
  (async function check() {
    try {
      const res = await fetch(API + '/agents/me', {
        headers: { 'Authorization': 'Bearer ' + token }
      });
      if (!res.ok) return;  // admin or token problem — let the page handle it
      const me = await res.json();
      // `phone` may be undefined if the migration hasn't run; treat any
      // non-empty string as set. Empty / null / undefined → gate.
      const phone = (me && typeof me.phone === 'string') ? me.phone.trim() : '';
      if (!phone) buildModal();
    } catch (_) { /* network glitch — skip gate so page isn't permanently locked */ }
  })();
})();
