"""Output formatting shared by the views: turning model output into safe HTML,
and framing Server-Sent Events. Pure functions — no requests, no database.
"""
import markdown  # server-side markdown -> HTML for the final rendered reply
import nh3  # HTML sanitizer applied to the rendered markdown before it reaches the browser

# nh3's defaults already cover every tag the markdown extensions emit; we only add
# back `class` on code blocks so fenced_code's language hint survives sanitizing.
ALLOWED_ATTRIBUTES = {
    **nh3.ALLOWED_ATTRIBUTES,
    'code': {'class'},
    'pre': {'class'},
}


def render_markdown(text):
    """Render the assistant's markdown reply to HTML for final display.

    The model's output is untrusted, so the generated HTML is sanitized before it
    reaches the browser — raw <script>/<iframe> tags and inline event handlers
    (onerror=, onload=, ...) are stripped rather than executed.
    """
    html = markdown.markdown(text, extensions=['fenced_code', 'tables'])
    return nh3.clean(html, attributes=ALLOWED_ATTRIBUTES)


def sse_frame(event, data):
    """Format a single Server-Sent Events frame. Multi-line data is split across
    multiple `data:` lines per the SSE spec (the client rejoins them with newlines)."""
    body = ''.join(f'data: {line}\n' for line in data.split('\n'))
    return f'event: {event}\n{body}\n'
