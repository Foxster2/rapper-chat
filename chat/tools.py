"""Tools the assistant can call during a reply.

One tool so far: web search, backed by Tavily. Kept apart from llm.py so the
model wiring and the capabilities stay separable — llm.py decides which tools an
agent gets, this module decides what each one does.

Tavily is called over plain `requests` rather than through langchain-tavily. The
API is a single POST, and going direct keeps the dependency list where it is and
leaves the result-trimming below under our control — which matters, because
every character returned here is re-sent to the model as input on the next turn.
"""
import requests
from django.conf import settings
from langchain_core.tools import tool

SEARCH_URL = 'https://api.tavily.com/search'

# Tavily already returns an extract of the relevant passage per hit rather than
# the whole page, so these caps are a backstop against an unusually long extract,
# not the primary trimming. Five results is the point where adding more stopped
# changing answers in testing while still costing a full re-send of the context.
MAX_RESULTS = 5
MAX_CHARS_PER_RESULT = 1200
TIMEOUT = 20


@tool
def web_search(query: str) -> str:
    """Search the web for current information and return extracts from the top results.

    Use this when the answer depends on something you can't reliably know: recent
    events, news, prices, release versions, or any fact that changes over time.
    Do not use it for stable knowledge you already have, or for questions about
    this conversation.

    Args:
        query: A focused search query, phrased as you would type it into a search
            engine rather than as a question to the user.
    """
    api_key = settings.TAVILY_API_KEY
    if not api_key:
        # Returned rather than raised: the agent can tell the user it couldn't
        # search and answer from what it knows, which beats failing the turn.
        return 'Web search is unavailable (no API key configured).'

    try:
        response = requests.post(
            SEARCH_URL,
            headers={'Authorization': f'Bearer {api_key}'},
            json={
                'query': query,
                'max_results': MAX_RESULTS,
                # 'basic' costs one Tavily credit against 'advanced''s two, and
                # returns the same extracts for the general questions this app
                # gets; 'advanced' mainly buys deeper crawling of thin pages.
                'search_depth': 'basic',
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.Timeout:
        return f'Web search timed out after {TIMEOUT}s. Answer from what you know, and say the search failed.'
    except requests.exceptions.RequestException as e:
        print(f'Tavily search failed: {e}')
        return 'Web search failed. Answer from what you know, and say the search failed.'
    except ValueError:
        return 'Web search returned an unreadable response. Answer from what you know, and say the search failed.'

    results = payload.get('results') or []
    if not results:
        return f'No results found for {query!r}.'

    blocks = []
    for i, item in enumerate(results[:MAX_RESULTS], start=1):
        content = (item.get('content') or '').strip()
        if len(content) > MAX_CHARS_PER_RESULT:
            content = content[:MAX_CHARS_PER_RESULT].rstrip() + '…'
        blocks.append(
            f"[{i}] {item.get('title') or 'Untitled'}\n"
            f"URL: {item.get('url') or ''}\n"
            f"{content}"
        )

    # Repeated here even though the system prompt says it too: buried at the end
    # of a few thousand characters of search text, a single earlier mention got
    # followed about one run in six. Restating it directly after the results,
    # phrased as an instruction rather than a preference, is what made it stick.
    return (
        '\n\n'.join(blocks)
        + '\n\nAnswer the question using these results. Cite every source you '
          'draw on as an inline markdown link, like [title](url). Then close '
          'with a "## Sources" heading and a bullet list of those same links, '
          'one per line.'
    )
