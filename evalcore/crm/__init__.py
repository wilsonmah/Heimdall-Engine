"""Outbound CRM integration: mapping, adapters (HubSpot, signed webhook) and
the asynchronous outbox dispatcher."""


class DeliveryError(Exception):
    """Base for adapter failures."""


class TransientError(DeliveryError):
    """Worth retrying: network error, 429, 5xx."""

    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


class PermanentError(DeliveryError):
    """Retrying will not help: bad credentials, rejected payload, misconfiguration."""
