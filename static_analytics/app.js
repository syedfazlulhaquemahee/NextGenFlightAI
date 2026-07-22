/* Skairova — Manager Portal JS */
(function () {
  'use strict';

  function applyChartDefaults() {
    if (typeof Chart === 'undefined') return;
    Chart.defaults.font.family = "'Inter', system-ui, sans-serif";
    Chart.defaults.font.size = 12;
    Chart.defaults.color = '#64748b';
    Chart.defaults.plugins.legend.position = 'bottom';
    Chart.defaults.plugins.legend.labels.usePointStyle = true;
    Chart.defaults.plugins.legend.labels.padding = 16;
    Chart.defaults.plugins.tooltip.padding = 10;
    Chart.defaults.plugins.tooltip.cornerRadius = 6;
    Chart.defaults.plugins.tooltip.backgroundColor = '#0f172a';
  }

  const PALETTE = {
    blue: '#2563eb',
    green: '#16a34a',
    amber: '#d97706',
    red: '#dc2626',
    purple: '#7c3aed',
    cyan: '#0891b2',
    pink: '#db2777',
    slate: '#475569',
  };

  const PALETTE_LIST = Object.values(PALETTE);

  function alpha(hex, a) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r},${g},${b},${a})`;
  }

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function getCsrfToken() {
    const meta = document.querySelector('meta[name="ngf-csrf-token"]');
    return meta ? String(meta.content || '') : '';
  }

  function redirectToLogin() {
    const nextPath = `${window.location.pathname}${window.location.search}`;
    window.location.href = `/login?next=${encodeURIComponent(nextPath)}`;
  }

  async function apiFetch(url, options) {
    const opts = options ? { ...options } : {};
    opts.headers = opts.headers ? { ...opts.headers } : {};
    opts.credentials = 'same-origin';

    const method = String(opts.method || 'GET').toUpperCase();
    if (method === 'POST' || method === 'PATCH' || method === 'PUT' || method === 'DELETE') {
      const csrf = getCsrfToken();
      if (csrf) {
        opts.headers['X-CSRF-Token'] = csrf;
      }
    }

    const response = await fetch(url, opts);
    if (response.status === 401) {
      redirectToLogin();
      throw new Error('unauthorized');
    }
    return response;
  }

  async function parseJson(response) {
    try {
      return await response.json();
    } catch (err) {
      return {};
    }
  }

  const _charts = {};

  function destroyChart(id) {
    if (_charts[id]) {
      _charts[id].destroy();
      delete _charts[id];
    }
  }

  function makeChart(id, config) {
    destroyChart(id);
    const el = document.getElementById(id);
    if (!el) return null;
    _charts[id] = new Chart(el, config);
    return _charts[id];
  }

  function renderTrends(id, data) {
    const labels = data.map((d) => d.day);
    makeChart(id, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: 'Searches',
            data: data.map((d) => d.searches),
            borderColor: PALETTE.blue,
            backgroundColor: alpha(PALETTE.blue, 0.08),
            borderWidth: 2,
            fill: true,
            tension: 0.35,
            pointRadius: 3,
          },
          {
            label: 'Bookings',
            data: data.map((d) => d.bookings),
            borderColor: PALETTE.green,
            backgroundColor: alpha(PALETTE.green, 0.08),
            borderWidth: 2,
            fill: true,
            tension: 0.35,
            pointRadius: 3,
          },
          {
            label: 'Signups',
            data: data.map((d) => d.signups),
            borderColor: PALETTE.amber,
            backgroundColor: alpha(PALETTE.amber, 0.08),
            borderWidth: 2,
            fill: true,
            tension: 0.35,
            pointRadius: 3,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: { grid: { display: false }, ticks: { maxTicksLimit: 10 } },
          y: { beginAtZero: true, grid: { color: '#f1f5f9' }, ticks: { precision: 0 } },
        },
      },
    });
  }

  function renderRevenue(id, data) {
    const labels = data.map((d) => d.day);
    makeChart(id, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          {
            label: 'Revenue ($)',
            data: data.map((d) => d.revenue),
            backgroundColor: alpha(PALETTE.green, 0.7),
            borderColor: PALETTE.green,
            borderWidth: 1,
            borderRadius: 4,
            yAxisID: 'yRev',
          },
          {
            label: 'Avg Value ($)',
            data: data.map((d) => d.avg_value),
            type: 'line',
            borderColor: PALETTE.blue,
            borderWidth: 2,
            tension: 0.3,
            pointRadius: 3,
            fill: false,
            yAxisID: 'yAvg',
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: { grid: { display: false } },
          yRev: { beginAtZero: true, position: 'left', grid: { color: '#f1f5f9' }, title: { display: true, text: 'Revenue ($)' } },
          yAvg: { beginAtZero: true, position: 'right', grid: { display: false }, title: { display: true, text: 'Avg ($)' } },
        },
      },
    });
  }

  function renderModes(id, data) {
    makeChart(id, {
      type: 'doughnut',
      data: {
        labels: data.map((d) => d.mode.toUpperCase()),
        datasets: [{
          data: data.map((d) => d.searches),
          backgroundColor: PALETTE_LIST,
          borderWidth: 2,
          borderColor: '#fff',
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '62%',
        plugins: { legend: { position: 'bottom' } },
      },
    });
  }

  function renderTripTypes(id, data) {
    makeChart(id, {
      type: 'doughnut',
      data: {
        labels: data.map((d) => d.trip_type.charAt(0).toUpperCase() + d.trip_type.slice(1)),
        datasets: [{
          data: data.map((d) => d.searches),
          backgroundColor: [PALETTE.blue, PALETTE.amber, PALETTE.purple, PALETTE.cyan],
          borderWidth: 2,
          borderColor: '#fff',
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '62%',
        plugins: { legend: { position: 'bottom' } },
      },
    });
  }

  function renderRoutes(id, data) {
    const top = data.slice(0, 12);
    makeChart(id, {
      type: 'bar',
      data: {
        labels: top.map((r) => `${r.origin} → ${r.destination}`),
        datasets: [
          {
            label: 'Searches',
            data: top.map((r) => r.searches),
            backgroundColor: alpha(PALETTE.blue, 0.75),
            borderColor: PALETTE.blue,
            borderWidth: 1,
            borderRadius: 4,
          },
          {
            label: 'Successful',
            data: top.map((r) => r.successful_searches),
            backgroundColor: alpha(PALETTE.green, 0.75),
            borderColor: PALETTE.green,
            borderWidth: 1,
            borderRadius: 4,
          },
        ],
      },
      options: {
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: { beginAtZero: true, grid: { color: '#f1f5f9' } },
          y: { grid: { display: false } },
        },
      },
    });
  }

  function renderCountries(id, data) {
    makeChart(id, {
      type: 'bar',
      data: {
        labels: data.map((c) => c.country),
        datasets: [
          {
            label: 'Events',
            data: data.map((c) => c.events),
            backgroundColor: alpha(PALETTE.blue, 0.7),
            borderRadius: 4,
          },
          {
            label: 'Searches',
            data: data.map((c) => c.searches),
            backgroundColor: alpha(PALETTE.amber, 0.7),
            borderRadius: 4,
          },
          {
            label: 'Bookings',
            data: data.map((c) => c.bookings),
            backgroundColor: alpha(PALETTE.green, 0.7),
            borderRadius: 4,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: { grid: { display: false } },
          y: { beginAtZero: true, grid: { color: '#f1f5f9' } },
        },
      },
    });
  }

  function loadTrends(id, days) {
    apiFetch(`/api/trends?days=${days}`)
      .then((r) => parseJson(r))
      .then((j) => renderTrends(id, j.trends || []))
      .catch(() => {});
  }

  function loadRevenue(id, days) {
    apiFetch(`/api/revenue?days=${days}`)
      .then((r) => parseJson(r))
      .then((j) => renderRevenue(id, j.series || []))
      .catch(() => {});
  }

  function loadEvents(limit) {
    const n = limit || 80;
    const tbody = document.getElementById('eventsTbody');
    if (!tbody) return;

    apiFetch(`/api/events?limit=${n}`)
      .then((r) => parseJson(r))
      .then((j) => {
        const evs = j.events || [];
        if (!evs.length) {
          tbody.innerHTML = '<tr><td colspan="10" class="empty-cell">No events yet.</td></tr>';
          return;
        }

        tbody.innerHTML = evs.map((ev) => {
          const success = ev.success
            ? '<span class="status-ok">✓</span>'
            : '<span class="status-fail">✗</span>';
          const route = (ev.origin && ev.destination)
            ? `<span class="route-tag">${esc(ev.origin)}</span><span class="route-arrow">→</span><span class="route-tag">${esc(ev.destination)}</span>`
            : '<span class="muted">—</span>';
          const anonLabel = ev.anon_label || (ev.anon_id ? `ANON-${String(ev.anon_id).slice(0, 8).toUpperCase()}` : 'ANON');
          const account = ev.account_email
            ? `<a class="link mono" href="/accounts?q=${encodeURIComponent(ev.account_email)}">${esc(ev.account_email)}</a>`
            : `<span class="tag tag-anon" title="${esc(ev.anon_id || '')}">${esc(anonLabel)}</span>`;

          const isBooking = String(ev.event_type || '').toLowerCase() === 'booking_completed';
          const action = isBooking
            ? '<span class="muted text-sm">Locked</span>'
            : `<button class="btn btn-danger btn-sm" onclick="NGF.deleteEvent(${Number(ev.id) || 0})">Delete</button>`;

          return `<tr>
            <td class="mono text-sm">${(ev.occurred_at || '').slice(0, 16)}</td>
            <td><span class="tag">${esc(ev.event_type)}</span></td>
            <td>${account}</td>
            <td>${ev.search_mode ? esc(ev.search_mode) : '<span class="muted">—</span>'}</td>
            <td>${route}</td>
            <td>${success}</td>
            <td class="num-cell">${ev.result_count || 0}</td>
            <td>${ev.location_country ? esc(ev.location_country) : '<span class="muted">—</span>'}</td>
            <td class="text-sm muted" title="${esc(ev.metadata_preview || '—')}">${esc(ev.metadata_preview || '—')}</td>
            <td>${action}</td>
          </tr>`;
        }).join('');
      })
      .catch(() => {
        if (tbody) tbody.innerHTML = '<tr><td colspan="10" class="empty-cell">Failed to load events.</td></tr>';
      });
  }

  function initSortableTables() {
    document.querySelectorAll('table.sortable').forEach((table) => {
      const headers = table.querySelectorAll('th[data-sort]');
      headers.forEach((th, idx) => {
        th.addEventListener('click', () => {
          const asc = !th.classList.contains('sort-asc');
          headers.forEach((h) => h.classList.remove('sort-asc', 'sort-desc'));
          th.classList.add(asc ? 'sort-asc' : 'sort-desc');
          const tbody = table.querySelector('tbody');
          const rows = Array.from(tbody.querySelectorAll('tr'));
          const type = th.dataset.sort;
          rows.sort((a, b) => {
            const av = (a.cells[idx] || {}).textContent.trim();
            const bv = (b.cells[idx] || {}).textContent.trim();
            if (type === 'num') {
              return asc ? (parseFloat(av) || 0) - (parseFloat(bv) || 0) : (parseFloat(bv) || 0) - (parseFloat(av) || 0);
            }
            return asc ? av.localeCompare(bv) : bv.localeCompare(av);
          });
          rows.forEach((r) => tbody.appendChild(r));
        });
      });
    });
  }

  function exportTable(tableId, filename) {
    const table = document.getElementById(tableId);
    if (!table) return;
    const rows = Array.from(table.querySelectorAll('tr'));
    const csv = rows.map((row) =>
      Array.from(row.querySelectorAll('th,td'))
        .map((cell) => `"${cell.textContent.trim().replace(/"/g, '""')}"`)
        .join(',')
    ).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || 'export.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  function initAccountToggles() {
    document.querySelectorAll('.account-toggle').forEach((el) => {
      el.addEventListener('change', function () {
        const email = this.dataset.email;
        const field = this.dataset.field;
        const value = this.checked;

        apiFetch(`/api/accounts/${encodeURIComponent(email)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ [field]: value }),
        })
          .then((r) => {
            if (r.ok) {
              showToast('Saved');
            } else {
              this.checked = !value;
              showToast('Save failed', true);
            }
          })
          .catch(() => {
            this.checked = !value;
            showToast('Save failed', true);
          });
      });
    });
  }

  function openEditModal(email, firstName, lastName) {
    document.getElementById('editEmail').value = email;
    document.getElementById('editFirstName').value = firstName;
    document.getElementById('editLastName').value = lastName;
    document.getElementById('editModal').style.display = 'flex';
  }

  function closeEditModal() {
    const modal = document.getElementById('editModal');
    if (modal) modal.style.display = 'none';
  }

  function saveEditModal() {
    const email = document.getElementById('editEmail').value;
    const first_name = document.getElementById('editFirstName').value.trim();
    const last_name = document.getElementById('editLastName').value.trim();

    apiFetch(`/api/accounts/${encodeURIComponent(email)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ first_name, last_name }),
    })
      .then((r) => {
        if (!r.ok) throw new Error('failed');
        return parseJson(r);
      })
      .then(() => {
        showToast('Account updated');
        closeEditModal();
        const row = document.querySelector(`tr[data-email="${email}"]`);
        if (row) {
          const span = row.querySelector('.editable-field[data-field="full_name"]');
          if (span) span.textContent = [first_name, last_name].filter(Boolean).join(' ') || '—';
        }
      })
      .catch(() => showToast('Update failed', true));
  }

  function clearSavedSearches(email) {
    if (!window.confirm(`Clear saved searches for ${email}?`)) return;
    apiFetch(`/api/accounts/${encodeURIComponent(email)}/saved-searches/clear`, { method: 'POST' })
      .then((r) => {
        if (!r.ok) throw new Error('failed');
        const row = document.querySelector(`tr[data-email="${email}"]`);
        if (row && row.cells[5]) row.cells[5].textContent = '0';
        showToast('Saved searches cleared');
      })
      .catch(() => showToast('Could not clear saved searches', true));
  }

  function clearAccountSearches(email) {
    if (!window.confirm(`Delete all tracked search events for ${email}?`)) return;
    apiFetch(`/api/accounts/${encodeURIComponent(email)}/searches/clear`, { method: 'POST' })
      .then((r) => parseJson(r).then((j) => ({ ok: r.ok, body: j })))
      .then((resp) => {
        if (!resp.ok) throw new Error('failed');
        const count = Number(resp.body.deleted_search_events || 0);
        showToast(`Deleted ${count} search event${count === 1 ? '' : 's'}`);
      })
      .catch(() => showToast('Could not delete account searches', true));
  }

  function requestAccountReset(email) {
    if (!window.confirm(`Send a password reset verification code to ${email}?`)) return;
    apiFetch(`/api/accounts/${encodeURIComponent(email)}/reset-request`, { method: 'POST' })
      .then((r) => parseJson(r).then((j) => ({ ok: r.ok, body: j })))
      .then((resp) => {
        if (!resp.ok) {
          const msg = resp.body.message || 'Could not send reset request';
          throw new Error(msg);
        }
        showToast('Reset verification code sent');
      })
      .catch((err) => showToast(err.message || 'Could not send reset request', true));
  }

  function deleteAccount(email) {
    if (!window.confirm(`Delete account ${email}? This cannot be undone.`)) return;
    apiFetch(`/api/accounts/${encodeURIComponent(email)}`, { method: 'DELETE' })
      .then((r) => parseJson(r).then((j) => ({ ok: r.ok, body: j })))
      .then((resp) => {
        if (!resp.ok) throw new Error('Could not delete account');
        const row = document.querySelector(`tr[data-email="${email}"]`);
        if (row) row.remove();
        showToast('Account deleted');
      })
      .catch((err) => showToast(err.message || 'Could not delete account', true));
  }

  function deleteEvent(eventId) {
    const id = Number(eventId || 0);
    if (!id) {
      showToast('Invalid event id', true);
      return;
    }
    if (!window.confirm(`Delete event #${id}?`)) return;

    apiFetch(`/api/events/${id}`, { method: 'DELETE' })
      .then((r) => parseJson(r).then((j) => ({ ok: r.ok, status: r.status, body: j })))
      .then((resp) => {
        if (!resp.ok) {
          if (resp.status === 403) {
            showToast('Booking events are read-only', true);
            return;
          }
          throw new Error('Could not delete event');
        }
        showToast('Event deleted');
        loadEvents();
      })
      .catch((err) => showToast(err.message || 'Could not delete event', true));
  }

  function clearBehavioralEvents() {
    if (!window.confirm('Clear current analytics behavior logs while keeping booking records?')) return;
    apiFetch('/api/analytics/clear-behavioral-events', { method: 'POST' })
      .then((r) => parseJson(r).then((j) => ({ ok: r.ok, body: j })))
      .then((resp) => {
        if (!resp.ok) throw new Error('Could not clear logs');
        const count = Number(resp.body.deleted_events || 0);
        showToast(`Cleared ${count} analytics log${count === 1 ? '' : 's'}`);
        window.setTimeout(() => window.location.reload(), 250);
      })
      .catch((err) => showToast(err.message || 'Could not clear logs', true));
  }

  function showToast(msg, isError) {
    const el = document.createElement('div');
    el.className = 'toast' + (isError ? ' error' : '');
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2800);
  }

  document.addEventListener('DOMContentLoaded', () => {
    applyChartDefaults();
    initSortableTables();
    initAccountToggles();

    const modal = document.getElementById('editModal');
    if (modal) {
      modal.addEventListener('click', (e) => {
        if (e.target === modal) closeEditModal();
      });
    }
  });

  window.NGF = {
    loadTrends,
    loadRevenue,
    renderModes,
    renderTripTypes,
    renderRoutes,
    renderCountries,
    loadEvents,
    exportTable,
    openEditModal,
    closeEditModal,
    saveEditModal,
    clearSavedSearches,
    clearAccountSearches,
    requestAccountReset,
    deleteAccount,
    deleteEvent,
    clearBehavioralEvents,
    showToast,
  };
})();
