"""Native mobile push APIs and transactional delivery.

Business producers call ``store.enqueue_notification`` in their own database
transaction. The worker sends only to the binding captured by that event.
"""
