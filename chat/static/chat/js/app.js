/* ══ ALPINE SHELL COMPONENT ══════════════════════════════════
   Owns local UI state: which conversation is active (drives the
   welcome ↔ chat view) and the theme. Server interactions are htmx. */
function chat() {
    return {
        activeId: null,
        theme: 'dark',
        sidebarOpen: true,
        init() {
            this.theme = localStorage.getItem('theme') || 'dark';
            document.documentElement.dataset.theme = this.theme;
            this.sidebarOpen = localStorage.getItem('sidebarOpen') !== 'false';
            /* The server tells us which conversation became active (select or start). */
            window.addEventListener('conversation-active', (e) => {
                this.activeId = e.detail.id;
                window.currentActiveId = e.detail.id;
                highlightActive();
            });
            window.addEventListener('go-welcome', () => this.newChat());
        },
        toggleTheme() {
            this.theme = this.theme === 'dark' ? 'light' : 'dark';
            document.documentElement.dataset.theme = this.theme;
            localStorage.setItem('theme', this.theme);
        },
        toggleSidebar() {
            this.sidebarOpen = !this.sidebarOpen;
            localStorage.setItem('sidebarOpen', this.sidebarOpen);
        },
        suggest(text) {
            const i = document.getElementById('welcome-input');
            i.value = text; i.focus(); autosize(i);
        },
        newChat() {
            this.activeId = null;
            window.currentActiveId = null;
            document.getElementById('chat-view').innerHTML = '';
            highlightActive();
            this.$nextTick(() => document.getElementById('welcome-input')?.focus());
        },
    };
}

/* ══ GLOBAL HELPERS (shared by Alpine + delegated handlers) ══ */
window.currentActiveId = null;

function autosize(el) {
    el.style.height = 'auto';
    const maxHeight = parseFloat(getComputedStyle(el).maxHeight);
    const overflowing = el.scrollHeight > maxHeight;
    el.style.height = (overflowing ? maxHeight : el.scrollHeight) + 'px';
    el.style.overflowY = overflowing ? 'auto' : 'hidden';
}

function scrollMessages() {
    const c = document.getElementById('messages-container');
    if (c) c.scrollTop = c.scrollHeight;
}

/* Active conversation gets the theme's secondary-color highlight */
function highlightActive() {
    document.querySelectorAll('.conversation-item').forEach(el => {
        const isActive = String(el.dataset.id) === String(window.currentActiveId);
        el.classList.toggle('bg-secondary/15', isActive);
        el.classList.toggle('text-secondary', isActive);
    });
}

/* ── Conversation actions popover (rename / delete), styled with daisyUI ── */
let currentPopover = null;
function closePopover() {
    if (currentPopover) { currentPopover.remove(); currentPopover = null; document.removeEventListener('click', closePopover); }
}
function showChatMenu(button) {
    closePopover();
    const id = button.dataset.id, title = button.dataset.title;
    const pop = document.createElement('div');
    pop.className = 'fixed z-50 w-32 rounded-box border border-base-300 bg-base-100 shadow-lg p-1 flex flex-col';

    const rename = document.createElement('button');
    rename.className = 'flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-base-200 w-full text-left';
    rename.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg> Rename';
    rename.onclick = (e) => { e.stopPropagation(); closePopover(); renameChat(id, title); };

    const del = document.createElement('button');
    del.className = 'flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-error hover:bg-error/10 w-full text-left';
    del.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg> Delete';
    del.onclick = (e) => { e.stopPropagation(); closePopover(); deleteChat(id); };

    pop.append(rename, del);
    document.body.appendChild(pop);
    currentPopover = pop;

    /* position: fixed → viewport-relative coordinates from the button */
    const r = button.getBoundingClientRect();
    pop.style.top = (r.bottom + 4) + 'px';
    pop.style.left = Math.max(8, r.right - 128) + 'px';
    setTimeout(() => document.addEventListener('click', closePopover), 0);
}

function renameChat(id, oldTitle) {
    const t = prompt('Rename chat:', oldTitle);
    if (t === null) return;
    const title = t.trim();
    if (!title || title === oldTitle) return;
    htmx.ajax('PUT', `/api/conversations/${id}/`, { values: { title }, target: '#conversations-list', swap: 'innerHTML' });
}

function deleteChat(id) {
    if (!confirm('Delete this chat?')) return;
    htmx.ajax('DELETE', `/api/conversations/${id}/`, { target: '#conversations-list', swap: 'innerHTML' })
        .then(() => { if (String(window.currentActiveId) === String(id)) window.dispatchEvent(new CustomEvent('go-welcome')); });
}

/* ══ DELEGATED EVENTS (work for htmx-swapped content) ══ */
document.addEventListener('click', (e) => {
    const btn = e.target.closest('.chat-actions-btn');
    if (btn) { e.stopPropagation(); e.preventDefault(); showChatMenu(btn); }
});
document.addEventListener('input', (e) => {
    if (e.target.matches('textarea.autosize')) autosize(e.target);
});
document.addEventListener('keydown', (e) => {
    if (e.target.matches('textarea.autosize') && e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        e.target.closest('form').requestSubmit();
    }
});

/* Autoscroll + re-highlight after htmx swaps and SSE token events */
document.body.addEventListener('htmx:afterSwap', (e) => {
    if (e.target && e.target.id === 'conversations-list') highlightActive();
    /* First streamed token arrived → drop the "thinking" dots for that bubble */
    if (e.target && e.target.classList && e.target.classList.contains('stream-tokens')) {
        e.target.parentElement.querySelector('.thinking')?.remove();
    }
    scrollMessages();
});
document.body.addEventListener('htmx:sseMessage', scrollMessages);

/* The __session cookie is a ~60s JWT that clerk-js refreshes on its own timer.
   A request can land right on that boundary and get a 401 before the refresh
   lands — force a fresh token (which updates the cookie) and retry once. The
   X-Retried-Auth header stops a second real 401 (e.g. actually signed out)
   from looping. */
document.body.addEventListener('htmx:responseError', async (e) => {
    const { xhr, requestConfig } = e.detail;
    if (xhr.status !== 401 || !window.Clerk?.session || requestConfig.headers['X-Retried-Auth']) return;
    await window.Clerk.session.getToken({ skipCache: true });
    htmx.ajax(requestConfig.verb, requestConfig.path, {
        source: requestConfig.elt,
        target: requestConfig.target,
        swap: requestConfig.swap,
        headers: { 'X-Retried-Auth': '1' },
    });
});

/* ══ LOAD CLERK ══════════════════════════════════════════════
   window.APP_CONFIG.clerkPublishableKey is set by a small inline script in
   index.html (server-rendered value can't live in this static file). */
(function () {
    const publishableKey = window.APP_CONFIG?.clerkPublishableKey || '';
    if (!publishableKey) {
        showError('CLERK_PUBLISHABLE_KEY is missing from your .env file.');
        return;
    }
    try {
        const domain = atob(publishableKey.split('_')[2]).slice(0, -1);
        const s = document.createElement('script');
        s.setAttribute('data-clerk-publishable-key', publishableKey);
        s.async = true;
        s.src = `https://${domain}/npm/@clerk/clerk-js@4/dist/clerk.browser.js`;
        s.crossOrigin = 'anonymous';
        s.addEventListener('load', initializeClerk);
        s.addEventListener('error', () => showError(`Failed to load Clerk script from ${domain}`));
        document.body.appendChild(s);
    } catch (e) {
        showError('Clerk Publishable Key format is invalid.');
    }
})();

async function initializeClerk() {
    try {
        await window.Clerk.load();
        if (!window.Clerk.user) {
            document.getElementById('loading-text').innerText = 'Redirecting to sign in…';
            window.Clerk.redirectToSignIn();
            return;
        }
        /* mount Clerk user button */
        window.Clerk.mountUserButton(document.getElementById('user-button'));

        /* welcome name */
        const firstName = window.Clerk.user.firstName || window.Clerk.user.username || '';
        document.getElementById('username-display').innerText = firstName;
        document.getElementById('welcome-name').innerText = firstName || 'there';

        /* show app */
        document.getElementById('loading-screen').style.display = 'none';
        document.getElementById('app-container').style.display = 'flex';

        /* load the sidebar list now that the Clerk session cookie is fresh */
        htmx.ajax('GET', '/api/conversations/partial/', { target: '#conversations-list', swap: 'innerHTML' });
    } catch (err) {
        showError(err.message || 'Clerk failed to initialize.');
    }
}

/* ══ ERROR HELPER ════════════════════════════════════════ */
function showError(msg) {
    const spinner = document.getElementById('loading-screen').querySelector('.loading');
    const text    = document.getElementById('loading-text');
    if (spinner) spinner.style.display = 'none';
    if (text)    text.innerHTML = `<span class="text-error font-semibold">Error</span><br><span class="text-xs">${msg}</span>`;
}
