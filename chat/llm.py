"""Everything that talks to the language model.

Kept apart from views.py so the LangChain surface lives in one place: the model
factory, the prompts, the reply chain, and the small title call. Nothing here
touches requests or responses, so it can be exercised directly.
"""
from django.conf import settings

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.output_parsers import StrOutputParser

SYSTEM_PROMPT = "You are a helpful, thoughtful AI assistant. Output response in clean, semantic markdown format."

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


def build_chain():
    """Build the LCEL chain (prompt | ChatOpenAI@OpenRouter | str) used by the
    streaming endpoint. Supports both .invoke() and .stream()."""
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        MessagesPlaceholder(variable_name="history"),
    ])
    return prompt | _chat_model(settings.OPENROUTER_MODEL) | StrOutputParser()


def build_history(conversation):
    """Convert stored messages into LangChain Human/AI messages, in order."""
    history = []
    for msg in conversation.messages.all():
        if msg.role == 'user':
            history.append(HumanMessage(content=msg.content))
        elif msg.content.strip():
            # A reply stopped before its first token is stored with empty content;
            # some providers reject an empty assistant turn, so leave it out of
            # the history rather than replaying it.
            history.append(AIMessage(content=msg.content))
    return history


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
