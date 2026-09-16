(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const flash = document.getElementById('flash-area');

  function show(message, kind = 'success') {
    if (!flash) return;
    const alert = document.createElement('div');
    alert.className = `alert alert-${kind} alert-dismissible`;
    alert.setAttribute('role', 'alert');
    alert.textContent = message;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close';
    close.addEventListener('click', () => alert.remove());
    alert.appendChild(close);
    flash.replaceChildren(alert);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrf,
        ...(options.headers || {}),
      },
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    return payload;
  }

  function formPayload(form) {
    const result = {};
    for (const element of form.elements) {
      if (!element.name || element.disabled) continue;
      let value = element.type === 'checkbox' ? element.checked : element.value;
      if (element.type === 'number' && value !== '') value = Number(value);
      const parts = element.name.split('.');
      let cursor = result;
      for (const part of parts.slice(0, -1)) cursor = cursor[part] ||= {};
      cursor[parts.at(-1)] = value;
    }
    return result;
  }

  document.querySelectorAll('[data-api-form]').forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      form.classList.add('api-busy');
      try {
        const result = await api(form.dataset.apiForm, {
          method: form.dataset.method || 'POST',
          body: JSON.stringify(formPayload(form)),
        });
        show(result.detail || 'Saved.');
        if (form.dataset.reload === 'true') window.location.reload();
      } catch (error) { show(error.message, 'danger'); }
      finally { form.classList.remove('api-busy'); }
    });
  });

  document.querySelectorAll('[data-runtime-config]').forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      form.classList.add('api-busy');
      try {
        const result = await api('/api/runtime-config', {
          method: 'PUT', body: JSON.stringify(formPayload(form)),
        });
        show(result.detail || 'Runtime configuration saved.', result.restart_required ? 'warning' : 'success');
      } catch (error) { show(error.message, 'danger'); }
      finally { form.classList.remove('api-busy'); }
    });
  });

  document.querySelectorAll('[data-project-toggle]').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await api('/api/projects/toggle', { method: 'POST', body: JSON.stringify({ project: button.dataset.projectToggle, enabled: button.dataset.enabled === 'true' }) });
        window.location.reload();
      } catch (error) { show(error.message, 'danger'); button.disabled = false; }
    });
  });

  document.querySelectorAll('[data-project-test]').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const result = await api('/api/projects/test', { method: 'POST', body: JSON.stringify({ project: button.dataset.projectTest }) });
        show(result.detail);
      } catch (error) { show(error.message, 'danger'); }
      finally { button.disabled = false; }
    });
  });

  document.querySelectorAll('[data-connection-test]').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      const original = button.textContent;
      button.textContent = 'Testing…';
      try {
        const result = await api(`/api/connections/${button.dataset.connectionTest}`, { method: 'POST', body: '{}' });
        show(result.detail);
      } catch (error) { show(error.message, 'danger'); }
      finally { button.disabled = false; button.textContent = original; }
    });
  });

  document.querySelectorAll('[data-requeue]').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        const result = await api(`/api/jobs/${button.dataset.requeue}/requeue`, { method: 'POST', body: '{}' });
        show(`Requeued ${result.job_id}; state=${result.state}`);
        window.setTimeout(() => window.location.reload(), 500);
      } catch (error) { show(error.message, 'danger'); button.disabled = false; }
    });
  });

  document.querySelectorAll('[data-service-toggle]').forEach((toggle) => {
    toggle.addEventListener('change', async () => {
      toggle.disabled = true;
      try {
        await api('/api/service', { method: 'POST', body: JSON.stringify({ enabled: toggle.checked }) });
        show(toggle.checked ? 'Review service enabled.' : 'Review service paused.', toggle.checked ? 'success' : 'warning');
      } catch (error) { toggle.checked = !toggle.checked; show(error.message, 'danger'); }
      finally { toggle.disabled = false; }
    });
  });
})();
