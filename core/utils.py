"""Utility functions for AI Control Plane."""


def validate_token_quantity(quantity):
    """Validate that token quantity is positive."""
    if quantity < 0:
        raise ValueError("Quantity cannot be negative")
    return quantity


def batch_usage_events(events, batch_size=100):
    """Split usage events into batches for processing."""
    batches = []
    for i in range(0, len(events), batch_size):
        batches.append(events[i:i + batch_size])
    return batches


def format_usd(amount):
    """Format amount as USD currency string."""
    return f"${amount:,.2f}"


def safe_divide(numerator, denominator, default=0):
    """Safely divide two numbers, returning default on division by zero."""
    if denominator == 0:
        return default
    return numerator / denominator
