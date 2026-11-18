"""Local smoke guard: no credential files and no outbound network calls."""
import os
import socket


def activate():
    """Keep the offline process independent of inherited model credentials."""
    for name in list(os.environ):
        if any(part in name.upper() for part in ("API_KEY", "AUTH_TOKEN")):
            os.environ.pop(name, None)
    os.environ["GOOGLE_API_KEY"] = "offline-placeholder"
    os.environ["OPENAI_API_KEY"] = "offline-placeholder"
    os.environ["AI_PROVIDER_FORMAT"] = "openai"
    os.environ["OPENAI_API_BASE"] = "http://127.0.0.1:9/v1"
    os.environ["OPENAI_MAX_RETRIES"] = "0"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["MAX_IMAGE_WORKERS"] = "1"
    os.environ["MAX_DESCRIPTION_WORKERS"] = "1"
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def allowed(address):
        return isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")

    def connect(sock, address):
        if not allowed(address):
            raise RuntimeError("Offline validation: outbound network is disabled")
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if not allowed(address):
            raise RuntimeError("Offline validation: outbound network is disabled")
        return original_connect_ex(sock, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
