const toast = document.querySelector('.toast');
function showToast(message) {
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add('is-visible');
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove('is-visible'), 2200);
}

const revealElements = [...document.querySelectorAll('.reveal')];
if ('IntersectionObserver' in window && revealElements.length) {
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add('revealed');
        observer.unobserve(entry.target);
      }
    });
  }, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 });
  revealElements.forEach((el) => observer.observe(el));
} else {
  revealElements.forEach((el) => el.classList.add('revealed'));
}

const installGlider = document.getElementById('tab-glider-install');
const installTabs = [...document.querySelectorAll('.tab-btn-install')];
const installPanels = [...document.querySelectorAll('[data-panel]')];
function activateInstallTab(name) {
  const activeTab = installTabs.find((tab) => tab.dataset.tab === name);
  if (!activeTab) return;
  installTabs.forEach((tab) => {
    const active = tab === activeTab;
    tab.classList.toggle('is-active', active);
    tab.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  installPanels.forEach((panel) => {
    const active = panel.dataset.panel === name;
    panel.classList.toggle('is-active', active);
    panel.hidden = !active;
  });
  if (installGlider) {
    installGlider.style.left = `${activeTab.offsetLeft}px`;
    installGlider.style.width = `${activeTab.offsetWidth}px`;
  }
}
installTabs.forEach((tab) => tab.addEventListener('click', () => activateInstallTab(tab.dataset.tab)));
window.addEventListener('load', () => {
  const active = installTabs.find((tab) => tab.classList.contains('is-active'));
  if (active) setTimeout(() => activateInstallTab(active.dataset.tab), 100);
});
window.addEventListener('resize', () => {
  const active = installTabs.find((tab) => tab.classList.contains('is-active'));
  if (active) activateInstallTab(active.dataset.tab);
});

document.querySelectorAll('[data-copy-target]').forEach((button) => {
  button.addEventListener('click', async () => {
    const target = document.getElementById(button.dataset.copyTarget);
    if (!target) return;
    try {
      await navigator.clipboard.writeText(target.textContent.trim());
      showToast('Code copied');
    } catch {
      showToast('Copy failed, select manually');
    }
  });
});

const gateLevel = document.getElementById('gate-level');
const gateApplyMode = document.getElementById('gate-apply-mode');
const applyModeLabel = document.getElementById('apply-mode-label');
const ownerAckContainer = document.getElementById('owner-ack-container');
const gateOwnerAck = document.getElementById('gate-owner-ack');
const btnRun = document.getElementById('btn-run-simulation');
const chatBox = document.getElementById('chat-box');
const serverLogs = document.getElementById('server-logs');
const toolButtons = [...document.querySelectorAll('.tool-btn')];
let selectedTool = 'status';

toolButtons.forEach((button) => button.addEventListener('click', () => {
  toolButtons.forEach((item) => item.classList.remove('is-active'));
  button.classList.add('is-active');
  selectedTool = button.dataset.tool;
}));

if (gateLevel) gateLevel.addEventListener('change', () => {
  const owner = gateLevel.value === 'owner';
  if (ownerAckContainer) ownerAckContainer.style.display = owner ? 'block' : 'none';
  if (!owner && gateOwnerAck) gateOwnerAck.checked = false;
});
if (gateApplyMode && applyModeLabel) gateApplyMode.addEventListener('change', () => {
  const direct = gateApplyMode.checked;
  applyModeLabel.textContent = direct ? 'direct' : 'dry_run';
  applyModeLabel.className = `mode-indicator ${direct ? 'direct' : 'dry-run'}`;
});

const levels = ['read_only', 'cron', 'skills', 'skills_config', 'workspace', 'owner'];
const toolPolicy = {
  status: { level: 'read_only', mutating: false, call: 'hermes_operator_status()' },
  cron: { level: 'cron', mutating: true, call: 'hermes_cron_run(...)' },
  workspace: { level: 'workspace', mutating: true, call: 'hermes_workspace_write(...)' },
  command: { level: 'owner', mutating: true, call: 'hermes_run_command(...)' },
};

function appendLog(text, className = 'system') {
  if (!serverLogs) return;
  const row = document.createElement('div');
  row.className = `log-line ${className}`;
  row.textContent = text;
  serverLogs.appendChild(row);
  serverLogs.scrollTop = serverLogs.scrollHeight;
}
function appendChat(html, className = 'bot-message') {
  if (!chatBox) return;
  const bubble = document.createElement('div');
  bubble.className = `chat-bubble ${className}`;
  bubble.innerHTML = html;
  chatBox.appendChild(bubble);
  chatBox.scrollTop = chatBox.scrollHeight;
}

if (btnRun) btnRun.addEventListener('click', () => {
  const policy = toolPolicy[selectedTool];
  const currentLevel = gateLevel ? gateLevel.value : 'read_only';
  const direct = gateApplyMode ? gateApplyMode.checked : false;
  const ownerAck = gateOwnerAck ? gateOwnerAck.checked : false;
  const allowed = levels.indexOf(currentLevel) >= levels.indexOf(policy.level);

  if (chatBox) chatBox.innerHTML = '';
  if (serverLogs) serverLogs.innerHTML = '';
  appendLog('[SYSTEM] Hermes GPT listening on 127.0.0.1:7677');
  appendLog('[SYSTEM] Streamable HTTP endpoint: /mcp');
  appendLog('[SYSTEM] Remote access requires a private/authenticated boundary');
  appendLog(`[MCP] Call tool: ${policy.call}`, 'incoming');
  appendChat(`<p>Requesting <code>${policy.call}</code> at level <code>${currentLevel}</code>.</p>`, 'user-message');

  if (!allowed) {
    appendLog(`[SECURITY] Rejected: requires ${policy.level}, current level is ${currentLevel}`, 'error');
    appendChat(`<p><strong>Blocked.</strong> This tool requires <code>${policy.level}</code> or higher.</p>`);
    showToast('Security gate blocked');
    return;
  }
  if (policy.mutating && !direct) {
    appendLog('[OPERATOR] Dry-run preview generated. No mutation applied.', 'success');
    appendChat('<p><strong>Dry-run only.</strong> The request is authorized for preview, but direct mutation is disabled.</p>');
    showToast('Dry-run preview generated');
    return;
  }
  if (policy.level === 'owner' && !ownerAck) {
    appendLog('[SECURITY] Owner-level mutation rejected: acknowledgment missing', 'error');
    appendChat('<p><strong>Owner acknowledgment required.</strong> Enable the explicit risk acknowledgment before direct owner-level execution.</p>');
    showToast('Owner acknowledgment required');
    return;
  }
  appendLog('[OPERATOR] Request authorized by current policy', 'success');
  appendChat('<p><strong>Authorized.</strong> The simulated request passed the active Operator policy.</p>');
  showToast('Policy check passed');
});

const footerYear = document.getElementById('footer-year');
if (footerYear) footerYear.textContent = String(new Date().getFullYear());
