"""No-op diagnostics for standard installations."""


def disarm():
    pass


class ServerRecording:
    def __init__(self, session):
        pass

    def audio(self, data):
        pass

    def frame(self, frame):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


async def maintenance(app):
    yield
