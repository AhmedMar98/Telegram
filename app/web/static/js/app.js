/* ============================================================
   Link Intelligence Platform — Frontend JS
   ============================================================ */

// Auto-refresh dashboard stats every 30s
if (window.location.pathname === '/') {
    setInterval(async () => {
        try {
            const r = await fetch('/api/v1/stats');
            if (!r.ok) return;
            const data = await r.json();
            // Update KPIs in place
            const kpis = document.querySelectorAll('.kpi-value');
            if (kpis.length >= 5) {
                kpis[0].textContent = data.links.total;
                kpis[1].textContent = data.links.alive;
                kpis[2].textContent = data.links.dead;
                kpis[3].textContent = data.links.new;
                kpis[4].textContent = data.links.classified || '—';
            }
        } catch (e) {
            console.warn('Stats refresh failed:', e);
        }
    }, 30000);
}

// Keyboard shortcut: '/' focuses search
document.addEventListener('keydown', (e) => {
    if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {
        const input = document.getElementById('searchInput');
        if (input) {
            e.preventDefault();
            input.focus();
        }
    }
});

// Enter-to-search on the search input
const searchInput = document.getElementById('searchInput');
if (searchInput) {
    searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            // Call the page's doSearch function if defined
            if (typeof doSearch === 'function') doSearch();
        }
    });
}
