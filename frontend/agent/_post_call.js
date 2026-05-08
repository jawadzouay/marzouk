// Post-call modal — shown on every agent page after the agent taps a
// call/WhatsApp button. The lead is saved to localStorage; on the next
// page-show / focus / visibility event, a full-screen modal opens that
// FORCES the agent to pick the call's outcome status. The modal cannot
// be closed without picking — the auto-mark-as-contacted feature has
// been removed precisely so we can collect a real outcome from the
// agent each time. Result powers ad-quality scoring.
//
// Public API (set on window):
//   MzPostCall.remember(leadId, leadName) — save before tel:/wa.me fires
//   MzPostCall.clear()                    — clear storage manually
//   MzPostCall.pending()                  — get current pending entry
//
// Page-level hook (optional): define window.onPostCallStatusUpdated to
// refresh the leads view after the modal saves. Both leads.html and
// dashboard wire it up.

(function () {
  const token = localStorage.getItem('token');
  const role  = localStorage.getItem('role') || '';
  if (!token || (role && role !== 'agent' && role !== 'manager')) return;

  const API = window.location.origin;
  const KEY = 'mz_pending_post_call';
  const TTL = 6 * 60 * 60 * 1000;  // 6 hours — covers a full work shift

  function getPending() {
    try {
      const raw = localStorage.getItem(KEY);
      if (!raw) return null;
      const o = JSON.parse(raw);
      if (!o || !o.leadId) return null;
      if (Date.now() - (o.ts || 0) > TTL) { localStorage.removeItem(KEY); return null; }
      return o;
    } catch (e) { return null; }
  }

  window.MzPostCall = {
    remember(leadId, leadName, leadPhone) {
      try {
        localStorage.setItem(KEY, JSON.stringify({
          leadId,
          leadName:  leadName  || '',
          leadPhone: leadPhone || '',
          ts: Date.now(),
        }));
      } catch (e) {}
    },
    clear()   { try { localStorage.removeItem(KEY); } catch (e) {} },
    pending() { return getPending(); },
  };

  // Order: most-aspirational first (gold + green) so the eye lands on
  // the outcomes the agent should be aiming for. Then mid-funnel
  // statuses, then dead-ends, then the catch-all مخصص.
  const STATUSES = [
    { id: 'registered',  lbl: 'مسجل',        emoji: '🎉', big: true  },
    { id: 'rdv',         lbl: 'موعد',        emoji: '📅', big: true  },
    { id: 'visits',      lbl: 'زيارة',       emoji: '🚪', big: false },
    { id: 'contacted',   lbl: 'تم التواصل',  emoji: '✅', big: false },
    { id: 'waiting',     lbl: 'في الانتظار', emoji: '⏳', big: false },
    { id: 'no_answer',   lbl: 'لا يجيب',     emoji: '📵', big: false },
    { id: 'bv',          lbl: 'بريد صوتي',   emoji: '🎙️', big: false },
    { id: 'pi',          lbl: 'غير مهتم',    emoji: '❌', big: false },
    { id: 'pe',          lbl: 'غير مؤهل',    emoji: '⚠️', big: false },
    { id: 'autre_ville', lbl: 'مدينة أخرى',  emoji: '🏙️', big: false },
    { id: 'over_40',     lbl: 'فوق 40',      emoji: '🎂', big: false },
    { id: 'contra',      lbl: 'كونترا',      emoji: '🚫', big: false },
    { id: 'custom',      lbl: 'مخصص',        emoji: '✏️', big: false },
  ];

  const STYLES = `
  .pco-overlay {
    position: fixed; inset: 0; z-index: 99998;
    background: linear-gradient(135deg, rgba(15,23,42,0.94), rgba(31,41,55,0.94));
    backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
    display: none; align-items: center; justify-content: center;
    padding: 16px;
    padding-top: max(16px, env(safe-area-inset-top));
    padding-bottom: max(16px, env(safe-area-inset-bottom));
    font-family: 'Cairo', 'Tajawal', sans-serif; direction: rtl;
  }
  .pco-overlay.show { display: flex; animation: pcoFade 0.25s ease-out; }
  @keyframes pcoFade { from { opacity: 0; } to { opacity: 1; } }

  .pco-card {
    background: white; border-radius: 24px;
    width: 100%; max-width: 600px; max-height: 96vh;
    overflow-y: auto; padding: 24px 22px;
    box-shadow: 0 30px 70px rgba(0,0,0,0.55);
    position: relative;
    animation: pcoSlide 0.4s cubic-bezier(0.34, 1.56, 0.64, 1);
  }
  @keyframes pcoSlide {
    from { opacity: 0; transform: translateY(40px) scale(0.93); }
    to   { opacity: 1; transform: translateY(0) scale(1); }
  }

  .pco-header { text-align: center; margin-bottom: 14px; }
  .pco-icon {
    font-size: 56px; line-height: 1; display: inline-block;
    animation: pcoRing 1.6s ease-in-out infinite;
  }
  @keyframes pcoRing {
    0%, 100% { transform: rotate(-8deg); }
    25%      { transform: rotate(8deg); }
    50%      { transform: rotate(-5deg); }
    75%      { transform: rotate(5deg); }
  }
  .pco-title { font-size: 22px; font-weight: 900; color: #0f172a; margin: 8px 0 4px; }
  .pco-name {
    font-size: 16px; font-weight: 800; color: #6366f1;
    min-height: 18px; word-break: break-word;
  }
  .pco-phone {
    font-size: 14px; font-weight: 700; color: #475569;
    margin-bottom: 12px; direction: ltr; letter-spacing: 0.5px;
    font-family: 'Cairo', monospace;
  }
  .pco-phone:empty { display: none; }
  .pco-help {
    font-size: 12.5px; color: #78350f; line-height: 1.7;
    background: linear-gradient(135deg, #fef3c7, #fde68a);
    padding: 11px 14px; border-radius: 12px;
    border: 1.5px solid #fbbf24; font-weight: 700;
  }

  .pco-grid {
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 8px; margin-top: 14px;
  }
  @media (max-width: 600px) {
    .pco-grid { grid-template-columns: repeat(3, 1fr); }
  }
  @media (max-width: 480px) {
    .pco-overlay { padding: 10px; }
    .pco-card { padding: 18px 14px; border-radius: 20px; }
    .pco-title { font-size: 18px; }
    .pco-icon { font-size: 44px; }
    .pco-name { font-size: 14px; }
    .pco-help { font-size: 11.5px; padding: 9px 12px; }
    .pco-grid { grid-template-columns: repeat(3, 1fr); gap: 7px; }
  }
  @media (max-width: 360px) {
    .pco-grid { grid-template-columns: repeat(2, 1fr); }
  }

  .pco-btn {
    background: white; border: 2px solid #e5e7eb; border-radius: 14px;
    padding: 10px 4px;
    display: flex; flex-direction: column; align-items: center; gap: 4px;
    font-family: inherit; cursor: pointer;
    transition: transform 0.15s cubic-bezier(0.34, 1.56, 0.64, 1),
                box-shadow 0.15s ease;
    min-height: 76px;
  }
  .pco-btn:hover  { transform: translateY(-2px); box-shadow: 0 6px 14px rgba(0,0,0,0.1); }
  .pco-btn:active { transform: scale(0.94); }
  .pco-btn-emoji { font-size: 22px; line-height: 1; }
  .pco-btn-lbl   { font-size: 12px; font-weight: 800; color: #374151; line-height: 1.25; }

  .pco-btn-rdv {
    background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%);
    border-color: #fbbf24;
  }
  .pco-btn-rdv .pco-btn-lbl { color: #78350f; }
  .pco-btn-rdv .pco-btn-emoji { font-size: 24px; }
  .pco-btn-registered {
    background: linear-gradient(135deg, #d1fae5 0%, #6ee7b7 100%);
    border-color: #10b981;
  }
  .pco-btn-registered .pco-btn-lbl { color: #064e3b; }
  .pco-btn-registered .pco-btn-emoji { font-size: 24px; }
  .pco-btn-visits      { background: #ecfdf5; border-color: #a7f3d0; }
  .pco-btn-visits .pco-btn-lbl { color: #065f46; }
  .pco-btn-contacted   { background: #dbeafe; border-color: #93c5fd; }
  .pco-btn-contacted .pco-btn-lbl { color: #1e40af; }
  .pco-btn-waiting     { background: #fef3c7; border-color: #fde68a; }
  .pco-btn-waiting .pco-btn-lbl { color: #78350f; }
  .pco-btn-no_answer   { background: #fef2f2; border-color: #fecaca; }
  .pco-btn-no_answer .pco-btn-lbl { color: #991b1b; }
  .pco-btn-bv          { background: #fffbeb; border-color: #fde68a; }
  .pco-btn-bv .pco-btn-lbl { color: #92400e; }
  .pco-btn-pi          { background: #fce7f3; border-color: #fbcfe8; }
  .pco-btn-pi .pco-btn-lbl { color: #9d174d; }
  .pco-btn-pe          { background: #f1f5f9; border-color: #cbd5e1; }
  .pco-btn-pe .pco-btn-lbl { color: #334155; }
  .pco-btn-autre_ville { background: #e0e7ff; border-color: #c7d2fe; }
  .pco-btn-autre_ville .pco-btn-lbl { color: #3730a3; }
  .pco-btn-over_40     { background: #ede9fe; border-color: #ddd6fe; }
  .pco-btn-over_40 .pco-btn-lbl { color: #5b21b6; }
  .pco-btn-contra      { background: #fce7f3; border-color: #fbcfe8; }
  .pco-btn-contra .pco-btn-lbl { color: #9d174d; }
  .pco-btn-custom      { background: #f5f3ff; border-color: #c4b5fd; }
  .pco-btn-custom .pco-btn-lbl { color: #5b21b6; }

  .pco-btn.picking {
    animation: pcoPick 0.55s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
    pointer-events: none;
  }
  @keyframes pcoPick {
    0%   { transform: scale(1); }
    35%  { transform: scale(1.18); box-shadow: 0 0 0 8px rgba(99,102,241,0.18); }
    100% { transform: scale(1); opacity: 0.85; }
  }
  .pco-btn.picking-big {
    animation: pcoPickBig 0.95s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
    pointer-events: none; position: relative; z-index: 2;
  }
  @keyframes pcoPickBig {
    0%   { transform: scale(1); }
    25%  { transform: scale(1.32) rotate(-4deg); box-shadow: 0 0 0 18px rgba(245,158,11,0.28); }
    55%  { transform: scale(1.42) rotate(4deg);  box-shadow: 0 0 0 36px rgba(245,158,11,0.16); }
    100% { transform: scale(1.12); }
  }

  .pco-rdv-panel {
    display: none; margin-top: 14px;
    background: linear-gradient(135deg, #fffbeb, #fef3c7);
    border: 2px solid #fbbf24; border-radius: 16px; padding: 16px;
    animation: pcoSlide 0.3s ease-out;
  }
  .pco-rdv-panel.show { display: block; }
  .pco-rdv-row { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
  .pco-rdv-row > div { flex: 1; min-width: 130px; }
  .pco-rdv-row label {
    font-size: 12px; font-weight: 800; color: #78350f;
    display: block; margin-bottom: 4px;
  }
  .pco-rdv-row input {
    width: 100%; padding: 12px 12px; border: 2px solid #fbbf24;
    border-radius: 10px; font-size: 16px; font-family: inherit;
    background: white; box-sizing: border-box;
  }
  .pco-rdv-row input:focus { outline: none; border-color: #d97706; }
  .pco-rdv-actions { display: flex; gap: 8px; }
  .pco-rdv-save {
    flex: 1; background: #10b981; color: white; border: none;
    padding: 13px; border-radius: 12px; font-weight: 800; font-size: 15px;
    font-family: inherit; cursor: pointer;
  }
  .pco-rdv-save:disabled { opacity: 0.6; cursor: wait; }
  .pco-rdv-cancel {
    background: white; color: #78350f; border: 2px solid #fde68a;
    padding: 13px 18px; border-radius: 12px; font-weight: 700; font-size: 14px;
    font-family: inherit; cursor: pointer;
  }

  .pco-confetti {
    position: fixed; inset: 0; pointer-events: none; z-index: 99999;
    overflow: hidden;
  }
  .pco-conf-piece {
    position: absolute; top: -20px; width: 10px; height: 14px;
    animation: pcoConfFall var(--dur, 2.4s) ease-in forwards;
    animation-delay: var(--delay, 0s);
  }
  @keyframes pcoConfFall {
    0%   { transform: translateY(-20px) rotate(0deg); opacity: 1; }
    100% { transform: translateY(105vh) rotate(720deg); opacity: 0; }
  }

  .pco-saving {
    position: absolute; inset: 0; background: rgba(255,255,255,0.85);
    display: none; align-items: center; justify-content: center;
    border-radius: 24px; z-index: 5;
    backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);
  }
  .pco-saving.show { display: flex; }
  .pco-saving-spinner {
    width: 50px; height: 50px;
    border: 4px solid #e5e7eb; border-top-color: #10b981;
    border-radius: 50%; animation: pcoSpin 0.8s linear infinite;
  }
  @keyframes pcoSpin { to { transform: rotate(360deg); } }
  .pco-error {
    color: #dc2626; font-size: 13px; font-weight: 800;
    text-align: center; margin-top: 10px; min-height: 16px;
  }

  /* Success checkmark — shown briefly after a normal-status save before the
     modal closes. The mark scales+rotates in, then a thank-you fades up. */
  .pco-success {
    position: absolute; inset: 0;
    display: none; flex-direction: column; align-items: center; justify-content: center;
    background: rgba(255,255,255,0.96); border-radius: 24px; z-index: 6;
    gap: 14px;
  }
  .pco-success.show { display: flex; }
  .pco-check-circle {
    width: 96px; height: 96px; border-radius: 50%;
    background: #10b981; display: flex; align-items: center; justify-content: center;
    animation: pcoCheckPop 0.5s cubic-bezier(0.34, 1.56, 0.64, 1);
    box-shadow: 0 12px 32px rgba(16, 185, 129, 0.35);
  }
  @keyframes pcoCheckPop {
    0%   { transform: scale(0) rotate(-45deg); opacity: 0; }
    60%  { transform: scale(1.15) rotate(0deg); opacity: 1; }
    100% { transform: scale(1) rotate(0deg);    opacity: 1; }
  }
  .pco-check-svg {
    width: 56px; height: 56px;
    stroke: white; stroke-width: 5;
    stroke-linecap: round; stroke-linejoin: round; fill: none;
  }
  .pco-check-svg path {
    stroke-dasharray: 60;
    stroke-dashoffset: 60;
    animation: pcoCheckDraw 0.4s ease-out 0.2s forwards;
  }
  @keyframes pcoCheckDraw { to { stroke-dashoffset: 0; } }
  .pco-thanks {
    font-size: 22px; font-weight: 900; color: #065f46;
    opacity: 0; transform: translateY(8px);
    animation: pcoThanksUp 0.4s ease-out 0.4s forwards;
  }
  .pco-thanks-sub {
    font-size: 13px; font-weight: 700; color: #6b7280;
    opacity: 0; transform: translateY(6px);
    animation: pcoThanksUp 0.4s ease-out 0.55s forwards;
  }
  @keyframes pcoThanksUp { to { opacity: 1; transform: translateY(0); } }

  /* Custom-status panel — same shell as RDV but a single text input */
  .pco-custom-panel {
    display: none; margin-top: 14px;
    background: linear-gradient(135deg, #f5f3ff, #ede9fe);
    border: 2px solid #c4b5fd; border-radius: 16px; padding: 16px;
    animation: pcoSlide 0.3s ease-out;
  }
  .pco-custom-panel.show { display: block; }
  .pco-custom-panel label {
    font-size: 12px; font-weight: 800; color: #5b21b6;
    display: block; margin-bottom: 6px;
  }
  .pco-custom-panel input {
    width: 100%; padding: 12px 14px; border: 2px solid #c4b5fd;
    border-radius: 10px; font-size: 15px; font-family: inherit;
    background: white; box-sizing: border-box; margin-bottom: 12px;
  }
  .pco-custom-panel input:focus { outline: none; border-color: #7c3aed; }
  .pco-custom-actions { display: flex; gap: 8px; }
  .pco-custom-save {
    flex: 1; background: #7c3aed; color: white; border: none;
    padding: 13px; border-radius: 12px; font-weight: 800; font-size: 15px;
    font-family: inherit; cursor: pointer;
  }
  .pco-custom-save:disabled { opacity: 0.6; cursor: wait; }
  .pco-custom-cancel {
    background: white; color: #5b21b6; border: 2px solid #ddd6fe;
    padding: 13px 18px; border-radius: 12px; font-weight: 700; font-size: 14px;
    font-family: inherit; cursor: pointer;
  }
  `;

  const styleEl = document.createElement('style');
  styleEl.textContent = STYLES;
  document.head.appendChild(styleEl);

  function buildModal() {
    const grid = STATUSES.map(s =>
      `<button class="pco-btn pco-btn-${s.id}" data-status="${s.id}" data-big="${s.big ? '1' : '0'}">
         <div class="pco-btn-emoji">${s.emoji}</div>
         <div class="pco-btn-lbl">${s.lbl}</div>
       </button>`
    ).join('');

    const wrap = document.createElement('div');
    wrap.innerHTML = `
      <div class="pco-overlay" id="pcoOverlay">
        <div class="pco-card">
          <div class="pco-header">
            <div class="pco-icon">📞</div>
            <div class="pco-title">اختر حالة المكالمة</div>
            <div class="pco-name" id="pcoLeadName"></div>
            <div class="pco-phone" id="pcoLeadPhone"></div>
            <div class="pco-help">المرجو إختيار الحالة المناسبة للمساعدة في تحسين جودة الإعلانات الخاصة بكم — وشكرا 🙏</div>
          </div>
          <div class="pco-grid" id="pcoGrid">${grid}</div>
          <div class="pco-rdv-panel" id="pcoRdvPanel">
            <div class="pco-rdv-row">
              <div>
                <label>📅 تاريخ الموعد</label>
                <input type="date" id="pcoRdvDate" />
              </div>
              <div>
                <label>🕒 الوقت</label>
                <input type="time" id="pcoRdvTime" />
              </div>
            </div>
            <div class="pco-rdv-actions">
              <button class="pco-rdv-save" id="pcoRdvSave">✓ حفظ الموعد</button>
              <button class="pco-rdv-cancel" id="pcoRdvCancel">رجوع للحالات</button>
            </div>
          </div>
          <div class="pco-custom-panel" id="pcoCustomPanel">
            <label for="pcoCustomInput">✏️ اكتب الحالة المخصصة</label>
            <input type="text" id="pcoCustomInput" maxlength="100" placeholder="مثال: طلب وقتاً للتفكير" />
            <div class="pco-custom-actions">
              <button class="pco-custom-save" id="pcoCustomSave">✓ حفظ الحالة</button>
              <button class="pco-custom-cancel" id="pcoCustomCancel">رجوع للحالات</button>
            </div>
          </div>
          <div class="pco-error" id="pcoError"></div>
          <div class="pco-saving" id="pcoSaving">
            <div class="pco-saving-spinner"></div>
          </div>
          <div class="pco-success" id="pcoSuccess">
            <div class="pco-check-circle">
              <svg class="pco-check-svg" viewBox="0 0 60 60" aria-hidden="true">
                <path d="M14 31 L26 43 L46 19" />
              </svg>
            </div>
            <div class="pco-thanks">شكراً لك! 🙏</div>
            <div class="pco-thanks-sub">تم حفظ الحالة بنجاح</div>
          </div>
        </div>
        <div class="pco-confetti" id="pcoConfetti"></div>
      </div>
    `;
    document.body.appendChild(wrap.firstElementChild);

    document.getElementById('pcoGrid').addEventListener('click', onStatusClick);
    document.getElementById('pcoRdvSave').addEventListener('click', onRdvSave);
    document.getElementById('pcoRdvCancel').addEventListener('click', cancelInlinePanels);
    document.getElementById('pcoCustomSave').addEventListener('click', onCustomSave);
    document.getElementById('pcoCustomCancel').addEventListener('click', cancelInlinePanels);
    document.getElementById('pcoCustomInput').addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); onCustomSave(); }
    });

    // Block Esc — modal must be answered.
    document.addEventListener('keydown', e => {
      const ov = document.getElementById('pcoOverlay');
      if (!ov || !ov.classList.contains('show')) return;
      if (e.key === 'Escape') e.preventDefault();
    });
  }

  function openModal(leadId, leadName, leadPhone) {
    if (!document.getElementById('pcoOverlay')) buildModal();
    document.getElementById('pcoLeadName').textContent  = leadName  || '';
    document.getElementById('pcoLeadPhone').textContent = leadPhone || '';
    document.getElementById('pcoOverlay').classList.add('show');
    document.body.style.overflow = 'hidden';
  }

  function closeModal() {
    const ov = document.getElementById('pcoOverlay');
    if (!ov) return;
    ov.classList.remove('show');
    document.body.style.overflow = '';
    cancelInlinePanels();
    document.getElementById('pcoError').textContent = '';
    const success = document.getElementById('pcoSuccess');
    if (success) success.classList.remove('show');
    document.querySelectorAll('.pco-btn').forEach(b => b.classList.remove('picking', 'picking-big'));
  }

  // Hide RDV / custom inline panels and bring the status grid back.
  function cancelInlinePanels() {
    const rdv    = document.getElementById('pcoRdvPanel');
    const cust   = document.getElementById('pcoCustomPanel');
    const grid   = document.getElementById('pcoGrid');
    if (rdv)  rdv.classList.remove('show');
    if (cust) cust.classList.remove('show');
    if (grid) grid.style.display = '';
    document.getElementById('pcoError').textContent = '';
    document.querySelectorAll('.pco-btn').forEach(b => b.classList.remove('picking', 'picking-big'));
  }

  async function onStatusClick(e) {
    const btn = e.target.closest('.pco-btn');
    if (!btn) return;
    const status = btn.dataset.status;
    const isBig  = btn.dataset.big === '1';
    document.getElementById('pcoError').textContent = '';

    btn.classList.add(isBig ? 'picking-big' : 'picking');

    if (status === 'rdv') {
      // Show date/time inline panel — don't save yet.
      fireConfetti(60, ['#fbbf24', '#f59e0b', '#fde68a']);
      tryPlaySound();
      setTimeout(() => {
        document.getElementById('pcoGrid').style.display = 'none';
        document.getElementById('pcoRdvPanel').classList.add('show');
        const today = new Date().toISOString().split('T')[0];
        const dateInput = document.getElementById('pcoRdvDate');
        if (!dateInput.value) dateInput.value = today;
      }, 350);
      return;
    }
    if (status === 'custom') {
      // Show free-text inline panel — don't save yet.
      setTimeout(() => {
        document.getElementById('pcoGrid').style.display = 'none';
        document.getElementById('pcoCustomPanel').classList.add('show');
        const inp = document.getElementById('pcoCustomInput');
        inp.value = '';
        setTimeout(() => inp.focus(), 80);
      }, 250);
      return;
    }
    if (status === 'registered') {
      fireConfetti(140, ['#10b981', '#fbbf24', '#3b82f6', '#ec4899', '#a855f7']);
      tryPlaySound();
    }

    setTimeout(() => commitStatus(status, null, null, null), isBig ? 700 : 380);
  }

  async function onRdvSave() {
    const date  = document.getElementById('pcoRdvDate').value;
    const time  = document.getElementById('pcoRdvTime').value;
    const errEl = document.getElementById('pcoError');
    if (!date) { errEl.textContent = 'أدخل تاريخ الموعد'; return; }
    document.getElementById('pcoRdvSave').disabled = true;
    await commitStatus('rdv', date, time || null, null);
    document.getElementById('pcoRdvSave').disabled = false;
  }

  async function onCustomSave() {
    const label = document.getElementById('pcoCustomInput').value.trim();
    const errEl = document.getElementById('pcoError');
    if (!label)            { errEl.textContent = 'اكتب الحالة المخصصة'; return; }
    if (label.length > 100) { errEl.textContent = 'الحالة طويلة جداً (الحد 100 حرف)'; return; }
    document.getElementById('pcoCustomSave').disabled = true;
    await commitStatus('custom', null, null, label);
    document.getElementById('pcoCustomSave').disabled = false;
  }

  function showSuccessAnimation(callback) {
    const success = document.getElementById('pcoSuccess');
    if (!success) { callback(); return; }
    success.classList.add('show');
    // 1.4s total: 0.5s pop + 0.4s draw + 0.4s thanks fade-up + 0.1s breathe.
    setTimeout(() => {
      success.classList.remove('show');
      callback();
    }, 1400);
  }

  async function commitStatus(status, rdvDate, rdvTime, customLabel) {
    const pending = getPending();
    if (!pending) { closeModal(); return; }
    document.getElementById('pcoSaving').classList.add('show');
    try {
      const body = { status };
      if (rdvDate)     body.rdv_date      = rdvDate;
      if (rdvTime)     body.rdv_time      = rdvTime;
      if (customLabel) body.custom_status = customLabel;
      const res = await fetch(API + `/ad-leads/${pending.leadId}/status`, {
        method: 'PATCH',
        headers: {
          'Authorization': 'Bearer ' + token,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || ('HTTP ' + res.status));
      }
      window.MzPostCall.clear();
      document.getElementById('pcoSaving').classList.remove('show');

      const finish = () => {
        closeModal();
        if (typeof window.onPostCallStatusUpdated === 'function') {
          try { window.onPostCallStatusUpdated(pending.leadId, status); } catch (_) {}
        }
      };

      // Big celebrations (موعد + مسجل) already had confetti + animation
      // on the button itself. For everything else, show the satisfying
      // checkmark + "شكراً لك" overlay before closing.
      const isBig = status === 'rdv' || status === 'registered';
      if (isBig) {
        setTimeout(finish, 900);
      } else {
        showSuccessAnimation(finish);
      }
    } catch (e) {
      document.getElementById('pcoSaving').classList.remove('show');
      document.getElementById('pcoError').textContent = 'فشل الحفظ — حاول مجدداً';
      document.querySelectorAll('.pco-btn').forEach(b => b.classList.remove('picking', 'picking-big'));
    }
  }

  function fireConfetti(count, colors) {
    const cont = document.getElementById('pcoConfetti');
    if (!cont) return;
    for (let i = 0; i < count; i++) {
      const el = document.createElement('div');
      el.className = 'pco-conf-piece';
      el.style.left = (Math.random() * 100) + '%';
      el.style.background = colors[Math.floor(Math.random() * colors.length)];
      el.style.setProperty('--delay', (Math.random() * 0.4) + 's');
      el.style.setProperty('--dur',   (1.7 + Math.random() * 1.2) + 's');
      el.style.transform = 'rotate(' + (Math.random() * 360) + 'deg)';
      if (Math.random() > 0.5) el.style.borderRadius = '50%';
      cont.appendChild(el);
      setTimeout(() => el.remove(), 3500);
    }
  }

  function tryPlaySound() {
    try {
      const a = new Audio('/sound effect.mp3');
      a.volume = 0.6;
      a.play().catch(() => {});
    } catch (_) {}
  }

  function maybeOpen() {
    const pending = getPending();
    if (!pending) return;
    const ov = document.getElementById('pcoOverlay');
    if (ov && ov.classList.contains('show')) return;
    openModal(pending.leadId, pending.leadName, pending.leadPhone);
  }

  document.addEventListener('visibilitychange', () => { if (!document.hidden) maybeOpen(); });
  window.addEventListener('focus',    maybeOpen);
  window.addEventListener('pageshow', maybeOpen);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', maybeOpen);
  } else {
    maybeOpen();
  }
})();
