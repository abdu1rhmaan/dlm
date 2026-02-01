class ExtractorRegistry:
    """
    Registry for managing available media extractors.
    """
    
    def __init__(self):
        self._extractors = []

    def register(self, extractor_class):
        """Register a new extractor class."""
        self._extractors.append(extractor_class)

    def get_extractor(self, url: str):
        """
        Find an extractor that supports the given URL.
        
        Returns:
            An instance of the extractor or None.
        """
        return None
