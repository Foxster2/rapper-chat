"""The second assistant: a critic that scores the user's prompt and the bot's
answer, and whose critiques are fed back into later generations.

Kept apart from llm.py because it is a different job with a different failure
mode. The chat model failing is visible and worth surfacing; the evaluator
failing is not worth interrupting a conversation over, so everything here
degrades to "no critique" rather than raising.
"""
import re

from django.conf import settings
from langchain_core.messages import HumanMessage, SystemMessage

from .llm import _chat_model

# Asked for on its own first line so it can be parsed off deterministically.
# Models will not reliably emit JSON at this size, but they will follow a fixed
# first line, and everything after it is free-form markdown for the reader.
_SCORE_FORMAT = (
    "Begin your reply with exactly one line of the form 'SCORE: n/10', where n "
    "is a whole number from 0 to 10. Then leave a blank line and write the "
    "critique itself as two or three short markdown bullets. Be specific and "
    "concrete -- name the thing that is missing, do not describe it in general "
    "terms. No preamble, no restating what was said, no praise on its own."
)

# Both critics are shown the conversation, and both are told the same two things
# about it, because both got them wrong in the same way: a follow-up read in
# isolation looks like it came out of nowhere, and a model with no browser will
# confidently call a citation fake if you let it.
_CONTEXT_RULES = (
    "You are shown the conversation that came before. Short follow-ups -- "
    "'summarize this', 'what about the second one', 'in metric' -- refer to "
    "what is already in that thread. Resolve them against it before judging "
    "anything. Never treat material as missing or invented because it was "
    "established in an earlier turn rather than repeated in this one.\n\n"
    "The assistant can search the web and cite what it finds. You cannot browse "
    "and you have no way to check a source. So never say a citation is fake, "
    "fabricated, broken, or unverifiable, never call content hallucinated on the "
    "grounds that you cannot confirm it, and never claim a cited page does not "
    "support what it is cited for -- you have not read it. The only citation "
    "fault you may report is a claim that carries no citation at all."
)

PROMPT_EVAL_SYSTEM = (
    "You evaluate how well a user's message is written as a question to an AI "
    "assistant. You are not answering it and you never address the assistant.\n\n"
    + _CONTEXT_RULES + "\n\n"
    "Judge one thing only: whether a competent assistant, reading this in "
    "context, would have to guess at something that changes the substance of "
    "the answer -- a location, a timeframe, a version, a subject the message "
    "does not identify.\n\n"
    "Presentation choices are not faults. Length, format, tone, which aspects "
    "to emphasise, how much detail to give: an assistant is expected to pick "
    "these sensibly, and a message is not worse for leaving them open. Do not "
    "ask for them. Brevity is not a fault either, and neither is leaning on the "
    "conversation.\n\n"
    "If the message can be answered well without asking anything back, it "
    "scores 8 or above -- most ordinary questions and follow-ups should. Reserve "
    "low scores for messages that genuinely cannot be answered as written, and "
    "if there is nothing substantive missing, say so instead of finding "
    "something.\n\n"
    "Address the user as 'you' and say what to add.\n\n" + _SCORE_FORMAT
)

RESPONSE_EVAL_SYSTEM = (
    "You evaluate an AI assistant's answer to a user's question. You are not "
    "rewriting it and you never address the user.\n\n"
    + _CONTEXT_RULES + "\n\n"
    "Judge whether it answers what was actually asked, whether claims that need "
    "support carry a citation, whether it handles genuine ambiguity instead of "
    "silently picking one reading, and whether it is padded. If the answer is "
    "right and complete, say so and score it high -- a good answer should score "
    "8 or above, and you are not required to find fault.\n\n"
    "Write the critique as instructions the assistant could act on next "
    "time.\n\n" + _SCORE_FORMAT
)

# How much of the material being judged to include. The evaluator does not need
# a whole long answer to tell whether it cites sources or dodges the question,
# and this call is pure overhead on top of the reply the user actually wanted.
MAX_EXCERPT_CHARS = 3000

_SCORE_RE = re.compile(r'^\s*(?:\*\*)?\s*SCORE\s*:?\s*(\d{1,2})\s*(?:/\s*10)?', re.IGNORECASE)


def is_enabled():
    """Whether critiques should be produced at all. Off means the conversation
    behaves exactly as it did before this existed."""
    return bool(settings.CHAT_EVALUATOR_ENABLED and settings.OPENROUTER_API_KEY)


def evaluate_prompt(question, transcript=''):
    """Score how well-posed the user's message is. -> (score|None, markdown)."""
    material = f'Conversation so far:\n{transcript}\n\n' if transcript else ''
    return _evaluate(
        PROMPT_EVAL_SYSTEM,
        f"{material}The user's new message to evaluate:\n{_excerpt(question)}",
    )


def evaluate_response(question, answer, transcript=''):
    """Score the assistant's answer to that message. -> (score|None, markdown).

    The transcript is not optional in practice. Without it a follow-up like
    'summarize this' reads as a request with no subject, and the critic calls a
    perfectly good summary of the previous turn a fabrication.
    """
    material = f'Conversation so far:\n{transcript}\n\n' if transcript else ''
    return _evaluate(
        RESPONSE_EVAL_SYSTEM,
        f"{material}The user then asked:\n{_excerpt(question)}\n\n"
        f"The assistant answered:\n{_excerpt(answer)}",
    )


def _excerpt(text):
    text = (text or '').strip()
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    return text[:MAX_EXCERPT_CHARS].rstrip() + '\n[...truncated]'


def _evaluate(system_prompt, user_content):
    """One critique call. Returns (None, '') on any failure -- a missing critique
    is a missing sidebar note, while a raised exception here would take down the
    reply the user is actually waiting for."""
    if not is_enabled():
        return None, ''
    try:
        raw = _chat_model(
            settings.OPENROUTER_EVALUATOR_MODEL, temperature=0.2,
            max_tokens=settings.CHAT_EVALUATOR_MAX_TOKENS,
        ).invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_content),
        ]).content
    except Exception as e:
        print(f'Evaluator call failed: {e}')
        return None, ''
    if not (raw or '').strip():
        # Almost always a reasoning model exhausting MAX_TOKENS before it starts
        # writing. Logged rather than swallowed: the symptom is a critique that
        # intermittently does not appear, which is invisible from the outside.
        print('Evaluator returned nothing -- raise CHAT_EVALUATOR_MAX_TOKENS '
              f'(currently {settings.CHAT_EVALUATOR_MAX_TOKENS})')
        return None, ''
    return parse_critique(raw)


def parse_critique(raw):
    """Split 'SCORE: n/10' off the front of a critique.

    The score line is dropped from the returned text because the UI shows it as
    a badge; leaving it in would print it twice. A reply that ignored the format
    is kept whole and scored None rather than discarded -- the prose is still
    useful, and refusing to show it would make an off-format model look like a
    broken feature.
    """
    text = (raw or '').strip()
    if not text:
        return None, ''

    lines = text.split('\n')
    match = _SCORE_RE.match(lines[0])
    if not match:
        return None, text

    score = min(int(match.group(1)), 10)
    return score, '\n'.join(lines[1:]).lstrip('\n')
