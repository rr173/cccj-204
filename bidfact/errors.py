"""Typed errors for the offline tender-fact processor."""


class BidFactError(Exception):
    """Base class for all expected, typed failures."""


class ManifestError(BidFactError):
    """The collection manifest is structurally invalid."""


class MaterialConflict(BidFactError):
    """Same immutable document_id maps to different byte content."""


class SliceMismatch(BidFactError):
    """A claimed [start, end) span does not match the page text bytes."""


class ExtractionError(BidFactError):
    """Extraction input for one document is malformed."""


class RelationValidationError(BidFactError):
    """Hard invalidity of a relation item (loop, future target ...)."""
