"""Only encrypted OS backends are accepted; no plaintext fallback."""
import json

from .core import SyncError

SERVICE = "telegram-lecturelog-sync"
ACCOUNT = "credentials-v1"


def backend():
    import keyring
    try:
        selected = keyring.get_keyring()
        candidates = getattr(selected, "backends", [selected])
        for candidate in candidates:
            module = type(candidate).__module__
            if module in ("keyring.backends.SecretService", "keyring.backends.macOS"):
                return candidate
    except Exception:
        pass
    raise SyncError("Недоступна системная связка ключей. В Ubuntu войдите в рабочий стол и разблокируйте GNOME Keyring; plaintext-хранилище не используется.")


def read() -> dict:
    try:
        value = backend().get_password(SERVICE, ACCOUNT)
        return json.loads(value) if value else {}
    except SyncError:
        raise
    except Exception:
        raise SyncError("Не удалось прочитать секреты из системной связки ключей.") from None


def update(**values):
    data = read()
    data.update(values)
    try:
        backend().set_password(SERVICE, ACCOUNT, json.dumps(data))
    except Exception:
        raise SyncError("Не удалось сохранить секреты в системной связке ключей.") from None
