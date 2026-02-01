from ..base import BaseExtractor
from ..result import ExtractResult

class FacebookExtractor(BaseExtractor):
    """Facebook media extractor."""
    
    def supports(self, url: str) -> bool:
        return False

    def extract(self, url: str) -> ExtractResult:
        raise NotImplementedError("Facebook extraction not implemented yet.")
