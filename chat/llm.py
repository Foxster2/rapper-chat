"""Everything that talks to the language model.

Kept apart from views.py so the LangChain surface lives in one place: the model
factory, the prompts, the reply chain, and the small title call. Nothing here
touches requests or responses, so it can be exercised directly.
"""
from django.conf import settings

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain_core.messages import (
    HumanMessage, AIMessage, SystemMessage, trim_messages,
)

from .tools import web_search

SYSTEM_PROMPT = (
    "You are a helpful, thoughtful AI assistant. Output response in clean, "
    "semantic markdown format.\n\n"
    "You can search the web. Search when the answer depends on information that "
    "changes over time or that you may not have — recent events, news, prices, "
    "current versions. Answer directly, without searching, when you already know "
    "the answer reliably or when the question is about this conversation.\n\n"
    "After searching, write the answer in prose and cite each source you used as "
    "an inline markdown link. Then end the reply with a '## Sources' heading "
    "followed by a bullet list of those sources, one markdown link per line. "
    "Add that section only when you actually searched — an answer from your own "
    "knowledge ends without it.\n\n"
    "Never output raw LaTeX such as \\boxed{}."
)

# Ceiling on model->tool->model round trips within a single reply. Each hop is a
# billed request, so this is the backstop against a model that keeps searching
# instead of answering. Counts every graph step, so the default 8 leaves room
# for roughly three searches before the graph stops.
RECURSION_LIMIT = settings.CHAT_AGENT_RECURSION_LIMIT

TITLE_PROMPT = (
    "You name chat conversations. Given the opening exchange, reply with a title "
    "of three to six words describing what the conversation is about. "
    "Reply with the title alone: no quotes, no surrounding punctuation, no "
    "prefix such as 'Title:', and no explanation."
)


def _chat_model(model_name, **kwargs):
    """A ChatOpenAI pointed at OpenRouter. Shared by the reply chain and the
    title generator so they can run on different models."""
    return ChatOpenAI(
        openai_api_base="https://openrouter.ai/api/v1",
        openai_api_key=settings.OPENROUTER_API_KEY,
        model_name=model_name,
        default_headers={
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "AI Chat Wrapper v1",
        },
        **kwargs,
    )


def build_agent():
    """The tool-using agent behind the streaming endpoint.

    A LangGraph agent rather than the old `prompt | model | StrOutputParser`
    chain: StrOutputParser drops tool calls, so that chain structurally could not
    call a tool no matter which model ran underneath it. The graph loops
    model -> tools -> model until the model answers instead of calling something.

    Streamed with stream_mode=["messages", "updates"] by the caller, which needs
    both the token stream and the node transitions -- the latter are what tell
    the UI a search is running, since the model emits no prose while deciding to
    call a tool.
    """
    return create_agent(
        _chat_model(settings.OPENROUTER_MODEL),
        [web_search],
        system_prompt=SYSTEM_PROMPT,
    )


def _critique_note(msg):
    """One evaluator message, phrased for the assistant rather than the reader.

    The two kinds are framed differently on purpose. A critique of an answer is
    a correction to apply next time. A critique of a question is not the
    assistant's fault at all -- it describes what the user left out, and the
    useful response to it is to cover the gap or ask, which is what the wording
    below asks for.
    """
    if msg.critique_of == 'prompt':
        lead = ("An evaluator reviewed the user's question above and found it "
                "underspecified. Take this into account when answering: either "
                "cover the missing ground explicitly or ask for it.")
    else:
        lead = ("An evaluator reviewed your previous answer. Apply this to "
                "everything you write from here on.")
    score = f' (scored {msg.score}/10)' if msg.score is not None else ''
    return f'{lead}{score}\n\n{msg.content.strip()}'


def _estimate_tokens(messages):
    """Rough token count for a list of messages: characters over four, plus a
    little per-message overhead for the role and framing.

    Deliberately an estimate rather than tiktoken. This only has to hold a
    budget, not bill anyone, and the models here are reached through OpenRouter
    -- so an OpenAI tokenizer would be an approximation of a different
    tokenizer anyway, bought at the price of fetching an encoding file on first
    use inside the container.
    """
    return sum(len(m.content) // 4 + 4 for m in messages)


def build_history(conversation):
    """Convert stored messages into LangChain messages, in order, keeping only
    the most recent CHAT_HISTORY_TOKEN_BUDGET tokens of them.

    Evaluator critiques are carried in as SystemMessages rather than as turns of
    the dialogue. They are not something anyone said: replaying a critique of the
    answer as an assistant turn would have the model treat its own scolding as
    something it had told the user, and replaying it as a user turn would invite
    it to reply to the critic instead of the person. As a system note it reads as
    what it is -- standing guidance that applies from that point in the
    conversation onward.
    """
    history = []
    for msg in conversation.messages.all():
        if msg.role == 'user':
            history.append(HumanMessage(content=msg.content))
        elif msg.role == 'evaluator':
            if msg.content.strip():
                history.append(SystemMessage(content=_critique_note(msg)))
        elif msg.content.strip():
            # A reply stopped before its first token is stored with empty content;
            # some providers reject an empty assistant turn, so leave it out of
            # the history rather than replaying it.
            history.append(AIMessage(content=msg.content))
    trimmed = trim_messages(
        history,
        max_tokens=settings.CHAT_HISTORY_TOKEN_BUDGET,
        token_counter=_estimate_tokens,
        strategy='last',
        # Dropping the oldest turns can leave a reply as the opening message,
        # which several providers reject; this discards it too.
        start_on='human',
        # create_agent supplies the system prompt itself, so there is none here
        # to preserve, and half a reply is worse context than none.
        include_system=False,
        allow_partial=False,
    )
    if trimmed:
        return trimmed
    # Nothing fit: the newest question is itself over budget, which a long
    # pasted log or stack trace manages easily. Trimming returns empty there,
    # and sending that would ask the model to reply to no conversation at all.
    # The question is the one thing that cannot be dropped, so send it alone and
    # over budget.
    for i in range(len(history) - 1, -1, -1):
        if isinstance(history[i], HumanMessage):
            return history[i:]
    return []


def generate_title(question, answer):
    """Ask the model for a short title describing an exchange.

    Returns '' if anything goes wrong — the caller keeps whatever title the
    conversation already has, since a bad title is worse than a plain one and a
    failure here must never surface in the chat.
    """
    try:
        # A ceiling against a runaway response, not a target -- the model stops
        # on its own after a few words. Kept well clear of that so a truncated
        # reply can never be mistaken for a title.
        raw = _chat_model(
            settings.OPENROUTER_TITLE_MODEL, temperature=0.3, max_tokens=512,
        ).invoke([
            SystemMessage(content=TITLE_PROMPT),
            HumanMessage(content=f'User: {question[:500]}\n\nAssistant: {answer[:500]}'),
        ]).content
    except Exception as e:
        print(f"Title generation failed: {e}")
        return ''
    return _clean_title(raw)


def _clean_title(raw):
    """Strip the decorations models put around a title, and reject anything that
    isn't one — the caller then keeps the existing name, which reads better than
    a sentence chopped off at the column limit."""
    title = (raw or '').strip().split('\n')[-1].strip()
    for prefix in ('chat title:', 'title:'):
        if title.lower().startswith(prefix):
            title = title[len(prefix):].strip()
    title = title.strip('"\'“”').rstrip('.').strip()
    return title if len(title) <= 60 else ''
