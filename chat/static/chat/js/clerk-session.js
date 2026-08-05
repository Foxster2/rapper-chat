/* ══ CLERK SESSION BOOTSTRAP ══════════════════════════════════
   Loads clerk-js and keeps the session alive. Every authenticated page has to
   include this, not just the chat shell: the __session cookie is a ~60s JWT
   that nothing but clerk-js refreshes, so a page without it lets the cookie
   quietly expire while the user reads. The billing pages are where that bites
   -- choosing a plan takes longer than the token lives, and the POST that
   follows arrives unauthenticated.

   window.clerkReady resolves once Clerk has loaded and a user is present, for
   pages that have their own work to do afterwards. It deliberately never
   resolves when we're redirecting to sign-in, since the page is going away.

   window.APP_CONFIG.clerkPublishableKey is set by a small inline script in
   each template (a server-rendered value can't live in this static file). */

let markClerkReady;
window.clerkReady = new Promise((resolve) => { markClerkReady = resolve; });

/* Surfaces a fatal Clerk failure on the loading screen when the page has one
   (the chat shell stays hidden behind it), and otherwise only to the console:
   the billing pages are server-rendered and remain readable without Clerk. */
function showClerkError(msg) {
    const screen = document.getElementById('loading-screen');
    if (!screen) {
        console.error(`Clerk: ${msg}`);
        return;
    }
    const spinner = screen.querySelector('.loading');
    const text = document.getElementById('loading-text');
    if (spinner) spinner.style.display = 'none';
    if (text) text.innerHTML = `<span class="text-error font-semibold">Error</span><br><span class="text-xs">${msg}</span>`;
}

(function () {
    const publishableKey = window.APP_CONFIG?.clerkPublishableKey || '';
    if (!publishableKey) {
        showClerkError('CLERK_PUBLISHABLE_KEY is missing from your .env file.');
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
        s.addEventListener('error', () => showClerkError(`Failed to load Clerk script from ${domain}`));
        document.body.appendChild(s);
    } catch (e) {
        showClerkError('Clerk Publishable Key format is invalid.');
    }
})();

async function initializeClerk() {
    try {
        await window.Clerk.load();
        if (!window.Clerk.user) {
            const text = document.getElementById('loading-text');
            if (text) text.innerText = 'Redirecting to sign in…';
            window.Clerk.redirectToSignIn();
            return;
        }
        markClerkReady();
    } catch (err) {
        showClerkError(err.message || 'Clerk failed to initialize.');
    }
}
