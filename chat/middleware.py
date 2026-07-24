import json
import time
import jwt
import requests
from jwt.algorithms import RSAAlgorithm
from django.conf import settings
from django.utils.deprecation import MiddlewareMixin

JWKS_CACHE_TTL = 3600  # seconds

class ClerkJWTMiddleware(MiddlewareMixin):
    _jwks_cache = None
    _jwks_fetched_at = 0

    def get_jwks(self, force_refresh=False):
        """Fetch and cache Clerk's public keys."""
        now = time.time()
        stale = (now - ClerkJWTMiddleware._jwks_fetched_at) > JWKS_CACHE_TTL
        if force_refresh or not ClerkJWTMiddleware._jwks_cache or stale:
            jwks_url = getattr(settings, 'CLERK_JWKS_URL', None)
            if not jwks_url:
                raise ValueError("CLERK_JWKS_URL setting is missing in settings.py")
            try:
                response = requests.get(jwks_url, timeout=5)
                response.raise_for_status()
                ClerkJWTMiddleware._jwks_cache = response.json()
                ClerkJWTMiddleware._jwks_fetched_at = now
            except Exception as e:
                print(f"Error fetching Clerk JWKS: {e}")
                return None
        return ClerkJWTMiddleware._jwks_cache

    def _find_key(self, jwks, kid):
        for key in jwks.get('keys', []):
            if key.get('kid') == kid:
                return key
        return None

    def process_request(self, request):
        """Processes each request before it matches a Django view."""
        request.clerk_user_id = None

        # clerk-js sets a short-lived, auto-refreshed __session JWT cookie on our origin.
        # All app requests are same-origin — including header-less SSE/EventSource, which
        # can't send an Authorization header — so we authenticate from this cookie alone.
        token = request.COOKIES.get('__session')

        if not token:
            return None

        # This middleware only *attempts* authentication — it never returns an error
        # response. Enforcement is the @require_clerk_auth decorator's job. This matters
        # for cookie auth: the browser sends __session on *every* request (including the
        # page load that boots clerk-js to refresh it), so a hard-fail on an expired
        # cookie would deadlock the very page that could renew it. On any failure we
        # simply leave request.clerk_user_id as None and let the request proceed.
        jwks = self.get_jwks()
        if not jwks:
            return None

        try:
            header = jwt.get_unverified_header(token)
            kid = header.get('kid')

            public_jwk = self._find_key(jwks, kid)
            if not public_jwk:
                jwks = self.get_jwks(force_refresh=True)
                public_jwk = self._find_key(jwks, kid) if jwks else None

            if not public_jwk:
                return None

            public_key = RSAAlgorithm.from_jwk(json.dumps(public_jwk))

            payload = jwt.decode(
                token,
                public_key,
                algorithms=['RS256'],
                options={"verify_aud": False},
                leeway=60
            )

            request.clerk_user_id = payload.get('sub')

        except jwt.InvalidTokenError:
            # Covers expired/malformed/invalid-signature tokens (ExpiredSignatureError
            # is a subclass). Stay unauthenticated rather than blocking the request.
            pass

        return None