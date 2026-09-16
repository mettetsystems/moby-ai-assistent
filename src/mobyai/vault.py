"""Local Secret Service vault. No plaintext fallback and no web secret-read API."""
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    usage: str

PROVIDERS = (
    Provider('huggingface', 'Hugging Face', 'Model downloads; environment and legacy token file take precedence.'),
    Provider('github', 'GitHub', 'Personal access token. Storage only; not automatically passed to Git or agents.'),
    Provider('gitlab', 'GitLab', 'Personal access token. Storage only; not automatically passed to Git or agents.'),
    Provider('google', 'Google Gemini / AI Studio', 'API key. Storage only; not a Google Scholar or Workspace credential.'),
    Provider('openai', 'OpenAI', 'API key. Storage only; does not change inference endpoints.'),
    Provider('anthropic', 'Anthropic', 'API key. Storage only; does not change inference endpoints.'),
    Provider('mistral', 'Mistral AI', 'API key. Storage only; does not change inference endpoints.'),
    Provider('npm', 'npm', 'Access token. Storage only; not automatically passed to package-install scripts.'),
    Provider('brave', 'Brave Search', 'MobyAI research searches; an existing environment key takes precedence.'),
    Provider('semantic_scholar', 'Semantic Scholar', 'MobyAI academic searches; an existing environment key takes precedence.'),
)
CATALOG = {provider.id: provider for provider in PROVIDERS}

class VaultError(ValueError):
    """Only fixed, credential-free messages should be raised to the console."""


def attributes(provider):
    if provider not in CATALOG:
        raise VaultError('Choose a supported credential provider.')
    return {'application': 'mobyai', 'kind': 'api-token', 'provider': provider, 'schema': '1'}


@contextmanager
def collection():
    connection = None
    try:
        import secretstorage
        connection = secretstorage.dbus_init()
        if not secretstorage.check_service_availability(connection):
            raise VaultError('Local Secret Service is unavailable. Start GNOME Keyring in your desktop session.')
        vault = secretstorage.get_default_collection(connection)
        if vault.is_locked():
            raise VaultError('Your desktop vault is locked. Unlock the login keyring in Passwords and Keys, then refresh.')
        yield vault
    except ImportError:
        raise VaultError('Install the vault extra: .venv/bin/python -m pip install -e ".[vault]"') from None
    except VaultError:
        raise
    except Exception:
        raise VaultError('Cannot access the local desktop vault. Open Passwords and Keys, check the default keyring, then retry.') from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


class Vault:
    def status(self):
        result = {'backend': 'Local Secret Service (GNOME Keyring on Fedora)',
                  'available': False, 'message': '',
                  'providers': [{'id':p.id,'name':p.name,'usage':p.usage,'configured':None} for p in PROVIDERS]}
        try:
            with collection() as vault:
                for item in result['providers']:
                    # Only item metadata is queried: no credential is decrypted for status.
                    item['configured'] = bool(list(vault.search_items(attributes(item['id']))))
            result['available'] = True
            result['message'] = 'Local vault is available. Saved values are never returned to the browser.'
        except VaultError as exc:
            result['message'] = str(exc)
        return result

    def save(self, provider, value):
        attrs = attributes(provider)
        if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > 8192:
            raise VaultError('Enter a non-empty API key or token, at most 8 KB.')
        value = value.strip()
        if any(ord(char) < 33 or ord(char) == 127 for char in value):
            raise VaultError('Use a single token without whitespace or control characters, not a JSON credentials file or private key.')
        with collection() as vault:
            vault.create_item('MobyAI — ' + CATALOG[provider].name, attrs, value.encode('utf-8'), replace=True)

    def remove(self, provider):
        attrs = attributes(provider)
        with collection() as vault:
            for item in vault.search_items(attrs):
                item.delete()

    def get(self, provider):
        attrs = attributes(provider)
        with collection() as vault:
            item = next(iter(vault.search_items(attrs)), None)
            return item.get_secret().decode('utf-8') if item else None


def optional_token(provider):
    """Read only for a trusted integration; vault absence preserves existing behavior."""
    try:
        return Vault().get(provider)
    except VaultError:
        return None
