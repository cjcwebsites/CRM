// ─── Modal helpers ─────────────────────────────────────────────────────────
function openModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add('open');
}
function closeModal(id) {
  const el = document.getElementById(id);
  if (el) {
    el.classList.remove('open');
    // reset hidden id fields so modal acts as "add" next time
    const hiddenId = el.querySelector('input[type=hidden]');
    if (hiddenId) hiddenId.value = '';
    // reset title
    const title = el.querySelector('.modal-header h3');
    if (title && title.dataset.default) title.textContent = title.dataset.default;
  }
}
function quickAddModal() { openModal('quick-add-modal'); }

// Close modal on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-overlay.open').forEach(m => {
      m.classList.remove('open');
    });
  }
});

// ─── Complete Activity ──────────────────────────────────────────────────────
async function completeActivity(id, btn) {
  const res = await fetch(`/api/activities/${id}/complete`, { method: 'PATCH' });
  if (!res.ok) return;
  const data = await res.json();
  // Update row style
  const row = document.getElementById(`act-${id}`) || document.getElementById(`dash-activity-${id}`);
  if (row) {
    if (data.completed) {
      row.classList.add('row-done');
      if (btn) btn.textContent = '↩ Reopen';
    } else {
      row.classList.remove('row-done');
      if (btn) btn.textContent = '✓ Done';
    }
    if (data.completed && row.id.startsWith('dash-')) {
      row.style.transition = 'opacity .4s';
      row.style.opacity = '0';
      setTimeout(() => row.remove(), 400);
    }
  }
}

// ─── Global Search ──────────────────────────────────────────────────────────
const searchInput = document.getElementById('global-search');
const searchResults = document.getElementById('search-results');
let searchTimer = null;

if (searchInput) {
  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const q = searchInput.value.trim();
    if (!q) { searchResults.classList.remove('show'); searchResults.innerHTML = ''; return; }
    searchTimer = setTimeout(() => doSearch(q), 220);
  });

  searchInput.addEventListener('focus', () => {
    if (searchInput.value.trim()) searchResults.classList.add('show');
  });

  document.addEventListener('click', e => {
    if (!searchInput.contains(e.target) && !searchResults.contains(e.target)) {
      searchResults.classList.remove('show');
    }
  });
}

async function doSearch(q) {
  const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
  if (!res.ok) return;
  const data = await res.json();
  let html = '';

  if (data.contacts.length) {
    html += '<div class="search-section-title">Contacts</div>';
    data.contacts.forEach(c => {
      html += `<a class="search-item" href="/contacts/${c.id}">
        <span>${c.name}</span>
        <span class="search-item-sub">${c.email || ''}</span>
      </a>`;
    });
  }
  if (data.companies.length) {
    html += '<div class="search-section-title">Companies</div>';
    data.companies.forEach(c => {
      html += `<a class="search-item" href="/companies/${c.id}">
        <span>${c.name}</span>
        <span class="search-item-sub">${c.industry || ''}</span>
      </a>`;
    });
  }
  if (data.deals.length) {
    html += '<div class="search-section-title">Deals</div>';
    data.deals.forEach(d => {
      html += `<a class="search-item" href="/deals?view=list&q=${encodeURIComponent(d.title)}">
        <span>${d.title}</span>
        <span class="search-item-sub">$${d.value.toLocaleString()}</span>
      </a>`;
    });
  }
  if (!html) {
    html = '<div class="search-item" style="color:#94a3b8">No results found</div>';
  }
  searchResults.innerHTML = html;
  searchResults.classList.add('show');
}
