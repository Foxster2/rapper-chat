"""Langfuse tracing: where each reply's time and money actually went.

Wired in the same spirit as evaluator.py. Telemetry is not the product, so
nothing in here may take a reply down with it: with no keys configured every
helper degrades to a no-op, and a Langfuse that is slow or down costs nothing
at the call site because the SDK batches spans and ships them on its own
thread.

The shape is one trace per turn, one session per conversation, which is what
Langfuse recommends for a chatbot -- nothing tells you upfront when a
conversation has ended, so a trace per conversation would stay open forever
and a turn is the largest unit that reliably closes.
"""
from contextlib import ExitStack, contextmanager

from django.conf import settings

# Whether init() got far enough to leave a usable client behind. Every helper
# reads it, so a failed or skipped setup turns this module into no-ops once
# rather than raising once per message.
_active = False


def is_configured():
    """Whether keys are present. The base URL has a default, but a key pair is
    project-specific and cannot be guessed."""
    return bool(settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY)


def init():
    """Start the Langfuse client. Called from ChatConfig.ready(), which is the
    first hook that runs after settings (and therefore load_dotenv) have been
    imported -- constructing the client any earlier reads the keys before the
    .env file has populated the environment.
    """
    global _active
    if _active or not is_configured():
        return
    try:
        from langfuse import Langfuse

        Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            base_url=settings.LANGFUSE_BASE_URL,
            environment=settings.LANGFUSE_ENVIRONMENT,
        )
    except Exception as e:
        print(f'Langfuse setup failed, continuing untraced: {e}')
        return

    # The prompt critique is evaluated on a worker thread (see stream_reply), and
    # OpenTelemetry's context is thread-local -- without this the critique lands
    # in a trace of its own instead of under the turn that asked for it. Not
    # fatal on its own, so a failure here still leaves tracing on.
    try:
        from opentelemetry.instrumentation.threading import ThreadingInstrumentor

        ThreadingInstrumentor().instrument()
    except Exception as e:
        print(f'Langfuse: thread context propagation unavailable: {e}')

    _active = True


class _NoopSpan:
    """Stands in for a span when tracing is off, so call sites can set an output
    unconditionally instead of guarding every one."""

    def update(self, **kwargs):
        pass


def callbacks():
    """The `callbacks` list for a LangChain invocation.

    Returns [] when tracing is off, which LangChain reads as "no callbacks"
    rather than as an error -- so call sites pass this straight into config
    without a branch. A fresh handler per call rather than a shared one: it
    picks up whichever trace is current when it is constructed, and this server
    handles conversations concurrently.
    """
    if not _active:
        return []
    try:
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler()]
    except Exception as e:
        print(f'Langfuse callback handler unavailable: {e}')
        return []


@contextmanager
def _observe(name, as_type, input=None, propagate=None):
    """Shared body of turn() and span(): enter the observation, and the
    attribute scope if one was asked for, yielding something with .update()
    either way.

    Only the setup is guarded. An exception raised by the caller's body is left
    to propagate so it both marks the span as errored and reaches the code that
    was actually meant to handle it.
    """
    if not _active:
        yield _NoopSpan()
        return
    stack = ExitStack()
    try:
        from langfuse import get_client, propagate_attributes

        span = stack.enter_context(get_client().start_as_current_observation(
            as_type=as_type, name=name, input=input,
        ))
        if propagate is not None:
            stack.enter_context(propagate_attributes(**propagate))
    except Exception as e:
        print(f'Langfuse span "{name}" could not be started: {e}')
        stack.close()
        yield _NoopSpan()
        return
    with stack:
        yield span


def turn(name, session_id=None, user_id=None, input=None, tags=None):
    """The root span of one trace, carrying the attributes that make it findable.

    Langfuse derives the trace's own input and output from this root span, which
    is why the user's message goes in here and the finished reply comes back
    through `.update(output=...)`. That pair is what the traces table shows at a
    glance, and it is what makes a bad turn findable without opening it.

    session_id is the conversation, so Langfuse's session view replays a whole
    thread in order; user_id is the Clerk subject, which is what per-user cost
    is grouped by.
    """
    return _observe(name, 'span', input=input, propagate={
        # Set explicitly rather than left to default off the span name: these
        # are read by dashboards and evaluators, so they are effectively an API
        # and should not move when a span is renamed.
        'trace_name': name,
        'session_id': session_id,
        'user_id': user_id,
        'tags': tags,
    })


def span(name, as_type='span', input=None):
    """A step inside an existing turn. as_type is the Langfuse observation type
    -- 'evaluator' for a critique, 'generation' for a model call -- and is worth
    setting because the type, not the name, is what the trace tree and the
    per-type analytics key off."""
    return _observe(name, as_type, input=input)
