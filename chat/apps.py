from django.apps import AppConfig


class ChatConfig(AppConfig):
    name = 'chat'

    def ready(self):
        # The earliest hook that runs after settings are imported, and therefore
        # after load_dotenv has put the Langfuse keys in the environment.
        # Constructing the client at module import time instead would read them
        # before the .env file had been applied.
        from . import tracing
        tracing.init()
