"""evalcore - headless, configuration-driven evaluation engine."""
from .pipeline import evaluate, validate_payload
from .trace import Trace
from .validation import validate_template

__version__ = "0.1.0"
__all__ = ["evaluate", "validate_payload", "validate_template", "Trace", "__version__"]
