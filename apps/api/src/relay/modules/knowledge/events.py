"""Domain events emitted by the ``knowledge`` module onto the transactional outbox.

Event names are stable contracts consumed by other modules and by webhooks
(RFC-001 §6.5). Safe to import cross-module (import-linter allows
``modules.* -> modules.*.events``).

The article lifecycle events double as the **Help Center ISR revalidation trigger**
(P0.8): on publish/unpublish/update/delete of a published article the service writes an
outbox row whose ``payload.paths`` names the Next.js routes to revalidate; the
``help-center-revalidate`` consumer (``relay.modules.knowledge.revalidation``) forwards
them to the site's on-demand revalidation webhook. Time-based ISR is the fallback.
"""

from __future__ import annotations

# Topic constants (``<module>.<aggregate>.<verb>``).
COLLECTION_CREATED = "knowledge.collection.created"
COLLECTION_UPDATED = "knowledge.collection.updated"
COLLECTION_DELETED = "knowledge.collection.deleted"

ARTICLE_CREATED = "knowledge.article.created"
ARTICLE_UPDATED = "knowledge.article.updated"
ARTICLE_PUBLISHED = "knowledge.article.published"
ARTICLE_UNPUBLISHED = "knowledge.article.unpublished"
ARTICLE_DELETED = "knowledge.article.deleted"

HELP_CENTER_UPDATED = "knowledge.help_center.updated"

# Aggregate names used on the outbox (``outbox.aggregate``).
AGGREGATE_ARTICLE = "article"
AGGREGATE_COLLECTION = "collection"
AGGREGATE_HELP_CENTER = "help_center"

# Article-lifecycle topics that trigger a Help Center ISR revalidation. Consumed by
# ``relay.modules.knowledge.revalidation``. A published article changing (or being
# unpublished/deleted) must refresh the live site; a draft edit must not.
REVALIDATION_TOPICS: frozenset[str] = frozenset(
    {ARTICLE_PUBLISHED, ARTICLE_UNPUBLISHED, ARTICLE_UPDATED, ARTICLE_DELETED}
)
