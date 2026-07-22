"""Shared kernel importable by every module (RFC-001 §6.2).

`relay.core` is deliberately outside the import-linter module-independence graph:
any module may depend on it, but it must never depend on a feature module.
"""
