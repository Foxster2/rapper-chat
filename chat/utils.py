"""Output formatting shared by the views: turning model output into safe HTML,
and framing Server-Sent Events. Pure functions — no requests, no database.
"""
import re

import markdown  # server-side markdown -> HTML for the final rendered reply
import nh3  # HTML sanitizer applied to the rendered markdown before it reaches the browser

# The heading the model is asked to end a searched answer with. Matched on the
# rendered heading rather than the markdown so a "Sources" written inside a code
# fence or a sentence cannot trigger it. h3 as well as h2 because models drift a
# level down when the answer above already uses headings.
SOURCES_HEADING = re.compile(r'<h[23]>\s*Sources\s*</h[23]>', re.IGNORECASE)

# nh3's defaults already cover every tag the markdown extensions emit; we only add
# back `class` on code blocks so fenced_code's language hint survives sanitizing,
# and `target` so citations can be opened without leaving the conversation.
ALLOWED_ATTRIBUTES = {
    **nh3.ALLOWED_ATTRIBUTES,
    'code': {'class'},
    'pre': {'class'},
    'a': {*nh3.ALLOWED_ATTRIBUTES['a'], 'target'},
}

# Forced onto every link rather than left to the model, which would remember it
# unevenly. nh3 pairs it with rel="noopener noreferrer" -- its link_rel default,
# and the thing that stops the opened page reaching back through window.opener.
# It also overrides a target the model wrote itself, so a reply cannot ask to
# replace the tab it is streaming into.
LINK_ATTRIBUTES = {'a': {'target': '_blank'}}


def render_markdown(text):
    """Render the assistant's markdown reply to HTML for final display.

    The model's output is untrusted, so the generated HTML is sanitized before it
    reaches the browser — raw <script>/<iframe> tags and inline event handlers
    (onerror=, onload=, ...) are stripped rather than executed. Link hrefs are
    untrusted too: nh3 drops any scheme outside its allowed set, so a citation
    pointing at javascript: survives only as unclickable text.
    """
    html = markdown.markdown(text, extensions=['fenced_code', 'tables'])
    clean = nh3.clean(
        html,
        attributes=ALLOWED_ATTRIBUTES,
        set_tag_attribute_values=LINK_ATTRIBUTES,
    )
    return _wrap_sources(clean)


def _wrap_sources(html):
    """Wrap a trailing `## Sources` heading and its list in a styleable div.

    Applied after sanitizing, not before: the markup being wrapped has already
    been through nh3, so the div this adds is the only untrusted-adjacent thing
    here and it carries no model input at all. Doing it in Python rather than
    CSS because the alternative -- an `id` on the heading -- would mean letting
    model-chosen ids into a page that already has ids of its own.
    """
    match = None
    for match in SOURCES_HEADING.finditer(html):
        pass  # the last one: an answer may mention sources before listing them
    if match is None:
        return html
    return f'{html[:match.start()]}<div class="sources">{html[match.start():]}</div>'


def sse_frame(event, data):
    """Format a single Server-Sent Events frame. Multi-line data is split across
    multiple `data:` lines per the SSE spec (the client rejoins them with newlines)."""
    body = ''.join(f'data: {line}\n' for line in data.split('\n'))
    return f'event: {event}\n{body}\n'
