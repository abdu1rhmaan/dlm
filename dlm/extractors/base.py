from abc import ABC, abstractmethod
from .result import ExtractResult

class BaseExtractor(ABC):
    """
    Abstract base class for all media extractors.
    
    This class defines the interface that platform-specific extractors 
    (YouTube, TikTok, etc.) must implement.

    CRITICAL BOUNDARIES:
    - Extractors ONLY identify media and fetch metadata.
    - Extractors do NOT download file content.
    - Extractors do NOT save files to disk.
    - Extractors do NOT determine format preferences (codecs, quality).
    """

    @abstractmethod
    def supports(self, url: str) -> bool:
        """
        Check if this extractor supports the given URL.
        
        Args:
            url: The URL to check.
            
        Returns:
            True if supported, False otherwise.
        """
        pass

    @abstractmethod
    def extract(self, url: str) -> ExtractResult:
        """
        Extract media information from the given URL.
        
        This method should perform network requests to metadata APIs
        or scrape pages to identify the content. It must return a
        valid ExtractResult object containing metadata and available streams.

        Args:
            url: The URL to extract from.
            
        Returns:
            ExtractResult: Unififed extraction result.

        Raises:
            NotImplementedError: Until implemented.
        """
        pass
