from enum import Enum


class LifetimeType(str, Enum):
    PERSISTENT = "persistent"  # running until explicitly stopped
    EPHEMERAL = "ephemeral"  # running for a predetermined time
    SINGLE_USE = "single_use"  # running until action is completed
    SESSION = "session"  # running for a session
