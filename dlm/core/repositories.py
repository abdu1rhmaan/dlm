from abc import ABC, abstractmethod
from typing import List, Optional
from .entities import Download

class DownloadRepository(ABC):
    @abstractmethod
    def save(self, download: Download) -> None:
        pass

    @abstractmethod
    def get(self, download_id: str) -> Optional[Download]:
        pass

    @abstractmethod
    def get_all(self) -> List[Download]:
        pass

    @abstractmethod
    def delete(self, download_id: str) -> None:
        pass

    @abstractmethod
    def get_browser_downloads(self) -> List[dict]:
        pass

    @abstractmethod
    def add_browser_download(self, url: str, filename: str, size: int, referrer: str, storage_state: str, user_agent: str, 
                             method: str = "GET", headers_json: str = "{}", cookies_json: str = "[]", source_url: str = None) -> int:
        pass

    @abstractmethod
    def get_browser_download(self, id: int) -> Optional[dict]:
        pass

    @abstractmethod
    def update_browser_download_size(self, capture_id: int, size: int) -> None:
        pass

    @abstractmethod
    def create_folder(self, name: str, parent_id: Optional[int]) -> int:
        pass

    @abstractmethod
    def get_folder(self, folder_id: int) -> Optional[dict]:
        pass

    @abstractmethod
    def get_folder_by_name(self, name: str, parent_id: Optional[int]) -> Optional[dict]:
        pass

    @abstractmethod
    def get_folders(self, parent_id: Optional[int]) -> List[dict]:
        pass

    @abstractmethod
    def update_folder_parent(self, folder_id: int, new_parent_id: Optional[int]) -> None:
        pass

    @abstractmethod
    def delete_folder(self, folder_id: int) -> None:
        pass

    @abstractmethod
    def get_all_by_folder(self, folder_id: Optional[int]) -> List[Download]:
        pass

    @abstractmethod
    def get_browser_downloads_by_folder(self, folder_id: Optional[int]) -> List[dict]:
        pass
