"""Password policy for the tenant's OpenNebula admin and its routers: the portal's policy (accounts.policy),
with the names of the person, the tenant and the organization, and the tenant's last passwords.
"""
from accounts import policy as _policy


def rules_text():
    return "; ".join(t for _k, t in _policy.rules())


def words_for(tenant_name="", org=None, user=None, extra=()):
    """Personal data a password must not contain a part of: the tenant, the organization, the person (accounts.policy)."""
    words = _policy.personal_words(tenant_name, *extra)
    if org is not None:
        words |= _policy.personal_words(org.short_name, org.name, getattr(org, "tax_id", ""))
    if user is not None:
        words |= _policy.words_for_user(user)
    return words


def problems(password, words=(), history_hashes=()):
    """List of what is wrong with a password (empty: it is fine)."""
    return _policy.problems(password, words, history_hashes)


def generate(length=16):
    """A random password that follows the policy."""
    return _policy.generate(length)
