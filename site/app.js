// 1. Toast Notification Helper
const toast = document.querySelector('.toast');
function showToast(message) {
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add('is-visible');
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove('is-visible'), 2500);
}

// 2. Scroll-triggered Page Reveals
const revealElements = [...document.querySelectorAll('.reveal')];
if ('IntersectionObserver' in window && revealElements.length) {
  const revealObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add('revealed');
        revealObserver.unobserve(entry.target);
      }
    });
  }, {
    root: null,
    rootMargin: '0px 0px -10% 0px',
    threshold: 0.05,
  });

  revealElements.forEach((el) => revealObserver.observe(el));
}

// 3. Tab Glider Alignment & Selection
const installGlider = document.getElementById('tab-glider-install');
const installTabs = [...document.querySelectorAll('.tab-btn-install')];
const installPanels = [...document.querySelectorAll('[data-panel]')];

function updateGlider(activeTab) {
  if (!installGlider || !activeTab) return;
  installGlider.style.left = `${activeTab.offsetLeft}px`;
  installGlider.style.width = `${activeTab.offsetWidth}px`;
}

function activateInstallTab(tabName) {
  const activeTab = installTabs.find((tab) => tab.dataset.tab === tabName);
  if (!activeTab) return;

  installTabs.forEach((tab) => {
    const active = tab === activeTab;
    tab.classList.toggle('is-active', active);
    tab.setAttribute('aria-selected', active ? 'true' : 'false');
  });

  installPanels.forEach((panel) => {
    const active = panel.dataset.panel === tabName;
    panel.classList.toggle('is-active', active);
    panel.hidden = !active;
  });

  updateGlider(activeTab);
}

installTabs.forEach((tab) => {
  tab.addEventListener('click', () => activateInstallTab(tab.dataset.tab));
});

window.addEventListener('resize', () => {
  const activeTab = installTabs.find((tab) => tab.classList.contains('is-active'));
  if (activeTab) updateGlider(activeTab);
});

// Initialize first install tab glider after page load
window.addEventListener('load', () => {
  const activeTab = installTabs.find((tab) => tab.classList.contains('is-active'));
  if (activeTab) {
    // Small timeout to let fonts / layouts calculate offsets correctly
    setTimeout(() => updateGlider(activeTab), 150);
  }
});

// 4. Secure Clipboard Copies
const copyButtons = [...document.querySelectorAll('[data-copy-target]')];
copyButtons.forEach((button) => {
  button.addEventListener('click', async () => {
    const targetId = button.dataset.copyTarget;
    const codeEl = document.getElementById(targetId);
    if (!codeEl) return;

    const codeText = codeEl.textContent.trim();
    if (!codeText) return;

    try {
      await navigator.clipboard.writeText(codeText);
      showToast('Code copied to clipboard');

      const originalText = button.textContent;
      button.textContent = 'Copied!';
      button.style.borderColor = 'var(--accent-teal)';
      button.style.color = 'var(--accent-teal)';

      window.setTimeout(() => {
        button.textContent = originalText;
        button.style.borderColor = '';
        button.style.color = '';
      }, 1500);
    } catch {
      showToast('Copy failed, select manually');
    }
  });
});

// 5. Live Interactive Sandbox Simulator
const btnRunSimulation = document.getElementById('btn-run-simulation');
const chatBox = document.getElementById('chat-box');
const serverLogs = document.getElementById('server-logs');
const toolButtons = [...document.querySelectorAll('.tool-btn')];

// State variables for active tool
let selectedTool = 'status';

// Handle tool selection click
toolButtons.forEach((btn) => {
  btn.addEventListener('click', () => {
    toolButtons.forEach((b) => b.classList.remove('is-active'));
    btn.classList.add('is-active');
    selectedTool = btn.dataset.tool;
  });
});

// Configure elements and listener for Level & Apply Mode
const gateLevel = document.getElementById('gate-level');
const gateApplyMode = document.getElementById('gate-apply-mode');
const applyModeLabel = document.getElementById('apply-mode-label');
const ownerAckContainer = document.getElementById('owner-ack-container');
const gateOwnerAck = document.getElementById('gate-owner-ack');

if (gateLevel) {
  gateLevel.addEventListener('change', () => {
    if (gateLevel.value === 'owner') {
      ownerAckContainer.style.display = 'block';
    } else {
      ownerAckContainer.style.display = 'none';
      if (gateOwnerAck) gateOwnerAck.checked = false;
    }
  });
}

if (gateApplyMode && applyModeLabel) {
  gateApplyMode.addEventListener('change', () => {
    const isDirect = gateApplyMode.checked;
    applyModeLabel.textContent = isDirect ? 'direct' : 'dry_run';
    applyModeLabel.className = `mode-indicator ${isDirect ? 'direct' : 'dry-run'}`;
  });
}

// Level hierarchy helper to check permissions
const levelHierarchy = ['read_only', 'cron', 'skills', 'skills_config', 'workspace', 'owner'];
function levelSatisfies(currentLevel, requiredLevel) {
  const currentIndex = levelHierarchy.indexOf(currentLevel);
  const requiredIndex = levelHierarchy.indexOf(requiredLevel);
  return currentIndex >= requiredIndex;
}

// Simulate events
if (btnRunSimulation) {
  btnRunSimulation.addEventListener('click', () => {
    btnRunSimulation.disabled = true;
    btnRunSimulation.style.opacity = '0.5';
    
    // Check gate configurations
    const currentLevel = gateLevel ? gateLevel.value : 'read_only';
    const isDirect = gateApplyMode ? gateApplyMode.checked : false;
    const applyMode = isDirect ? 'direct' : 'dry_run';
    const isOwnerAckSet = gateOwnerAck ? gateOwnerAck.checked : false;

    // Define tool requirements
    let reqLevel = 'read_only';
    let isMutating = false;
    let userPromptText = '';
    let toolCallText = '';

    if (selectedTool === 'status') {
      reqLevel = 'read_only';
      isMutating = false;
      userPromptText = 'Can you check my operator policy status?';
      toolCallText = 'hermes_operator_status()';
    } else if (selectedTool === 'cron') {
      reqLevel = 'cron';
      isMutating = true;
      userPromptText = 'Run the database backup cron job now.';
      toolCallText = 'hermes_cron_run(job_id="db-backup-02", profile="default")';
    } else if (selectedTool === 'workspace') {
      reqLevel = 'workspace';
      isMutating = true;
      userPromptText = 'Write a basic index.html into the workspace folder.';
      toolCallText = 'hermes_workspace_write(path="index.html", content="<!DOCTYPE html>...")';
    } else if (selectedTool === 'command') {
      reqLevel = 'owner';
      isMutating = true;
      userPromptText = 'Deploy my site by running the build command in the terminal.';
      toolCallText = 'hermes_run_command(command="vite build")';
    }

    // Clear previous logs and append connection startup
    chatBox.innerHTML = '';
    serverLogs.innerHTML = `
      <div class="log-line system">[SYSTEM] Server started on 127.0.0.1:4750</div>
      <div class="log-line system">[SYSTEM] Policy initialized: level=${currentLevel}, mode=${applyMode}</div>
      <div class="log-line system">[SYSTEM] Tunnel active: https://hermes-gpt-tunnel.trycloudflare.com/mcp</div>
    `;

    // A. Add user message to Chat
    const userMessageDiv = document.createElement('div');
    userMessageDiv.className = 'chat-bubble user-message';
    userMessageDiv.innerHTML = `<p>${userPromptText}</p>`;
    chatBox.appendChild(userMessageDiv);
    chatBox.scrollTop = chatBox.scrollHeight;

    // B. Add ChatGPT loading indicator
    const botIndicatorDiv = document.createElement('div');
    botIndicatorDiv.className = 'chat-bubble bot-message mcp-indicator-wrapper';
    botIndicatorDiv.innerHTML = `
      <p>Consulting local sidecar policy...</p>
      <div class="mcp-indicator">
        <span></span> Calling local sidecar...
      </div>
    `;
    
    setTimeout(() => {
      chatBox.appendChild(botIndicatorDiv);
      chatBox.scrollTop = chatBox.scrollHeight;
    }, 600);

    // C. Server connection log
    setTimeout(() => {
      const connLog = document.createElement('div');
      connLog.className = 'log-line incoming';
      connLog.innerHTML = `[SSE] GET /mcp/stream - Connection established`;
      serverLogs.appendChild(connLog);
      
      const reqLog = document.createElement('div');
      reqLog.className = 'log-line incoming';
      reqLog.innerHTML = `[MCP] Call tool: ${toolCallText}`;
      serverLogs.appendChild(reqLog);
      serverLogs.scrollTop = serverLogs.scrollHeight;
    }, 1200);

    // D. Tool Execution & Gate evaluation
    setTimeout(() => {
      const execLog = document.createElement('div');
      const botResponse = document.createElement('div');
      botResponse.className = 'chat-bubble bot-message';
      
      // Remove loading indicator bubble
      botIndicatorDiv.remove();

      // Check level constraint
      const hasLevel = levelSatisfies(currentLevel, reqLevel);

      if (!hasLevel) {
        execLog.className = 'log-line error';
        execLog.innerHTML = `[SECURITY] Gated call rejected: hermes_${selectedTool} is disabled.\n[SECURITY] Reason: Tool requires level "${reqLevel}" but current level is "${currentLevel}".`;
        serverLogs.appendChild(execLog);
        
        botResponse.innerHTML = `<p><strong>Error: Security Gate Blocked.</strong> The tool execution was rejected because the sidecar server is configured with <code>HERMES_GPT_OPERATOR_LEVEL=${currentLevel}</code>, but this action requires level <code>${reqLevel}</code> or higher.</p>`;
        showToast('Security gate blocked: insufficient level');
      } 
      
      else if (isMutating && applyMode === 'dry_run') {
        execLog.className = 'log-line executing';
        execLog.innerHTML = `[OPERATOR] Dry-run preview: Planned change is safe. (dry_run=true)\n`;
        serverLogs.appendChild(execLog);
        
        setTimeout(() => {
          const planOut = document.createElement('div');
          planOut.className = 'log-line success';
          planOut.innerHTML = `[PLAN] Success. No changes were applied since HERMES_GPT_OPERATOR_APPLY_MODE is dry_run.`;
          serverLogs.appendChild(planOut);
          serverLogs.scrollTop = serverLogs.scrollHeight;
        }, 600);

        botResponse.innerHTML = `
          <p><strong>Dry-Run Preview:</strong> I have generated an execution plan. Because your sidecar is running in <code>dry_run</code> mode, no actual changes were made to your machine.</p>
          <pre style="margin-top: 8px; font-family: var(--font-mono); font-size: 11px; background: rgba(255,255,255,0.06); padding: 8px; border-radius: 6px;">[DRY RUN PLAN]
Target: hermes_${selectedTool}
Policy: level=${currentLevel}
Action: Would perform mutation safely.</pre>
        `;
        showToast('Dry-run preview generated');
      } 
      
      else {
        // Direct execution mode
        if (reqLevel === 'owner' && !isOwnerAckSet) {
          execLog.className = 'log-line error';
          execLog.innerHTML = `[SECURITY] Owner tool rejected: requires risk acknowledgment.\n[SECURITY] To enable, set: HERMES_GPT_OWNER_ACK="I_UNDERSTAND_THIS_CAN_MUTATE_MY_MACHINE"`;
          serverLogs.appendChild(execLog);
          
          botResponse.innerHTML = `<p><strong>Error: Risk Acknowledgment Required.</strong> This is an owner-level tool. To run it directly, you must toggle the <code>HERMES_GPT_OWNER_ACK</code> acknowledgment switch to verify you understand the risks.</p>`;
          showToast('Owner acknowledgment required');
        } else {
          // Success direct execution
          execLog.className = 'log-line success';
          if (selectedTool === 'status') {
            execLog.innerHTML = `[SYSTEM] Operator level: ${currentLevel}\n[SYSTEM] Apply mode: ${applyMode}\n[SYSTEM] Status: OK`;
            botResponse.innerHTML = `<p>I have queried your sidecar status. The server is healthy, running at level <code>${currentLevel}</code> in <code>${applyMode}</code> mode.</p>`;
            showToast('Operator status fetched');
          } else if (selectedTool === 'cron') {
            execLog.innerHTML = `[CRON] Run job "db-backup-02" completed successfully.\n[SYSTEM] exited with code 0`;
            botResponse.innerHTML = `<p>I have executed the database backup cron job successfully. Backup file saved under the profile directory.</p>`;
            showToast('Cron task executed successfully');
          } else if (selectedTool === 'workspace') {
            execLog.innerHTML = `[FILE] Scoped write successful: index.html written under workspace.`;
            botResponse.innerHTML = `<p>I have written the <code>index.html</code> file directly into your workspace scoped path.</p>`;
            showToast('Workspace file written successfully');
          } else if (selectedTool === 'command') {
            execLog.innerHTML = `[TERMINAL] Executing: vite build\n[SHELL] ✓ built in 480ms\n[SYSTEM] process exited with code 0`;
            botResponse.innerHTML = `<p>I have successfully executed the terminal command <code>vite build</code>. The build was completed and client assets are generated.</p>`;
            showToast('Terminal command executed successfully');
          }
          serverLogs.appendChild(execLog);
        }
      }

      chatBox.appendChild(botResponse);
      chatBox.scrollTop = chatBox.scrollHeight;
      serverLogs.scrollTop = serverLogs.scrollHeight;
      
      // Re-enable trigger button
      btnRunSimulation.disabled = false;
      btnRunSimulation.style.opacity = '1';
    }, 2800);
  });
}

// 6. Set current year in Footer
const footerYearEl = document.getElementById('footer-year');
if (footerYearEl) {
  footerYearEl.textContent = String(new Date().getFullYear());
}
