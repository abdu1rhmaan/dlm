from dlm.core.entities import DownloadState
from dlm.core.repositories import DownloadRepository
from dlm.app.services import DownloadService

class AutoResumeService:
    def __init__(self, repository: DownloadRepository, download_service: DownloadService):
        self.repository = repository
        self.download_service = download_service
    
    def resume_interrupted_downloads(self):
        """Resume all downloads that were interrupted."""
        all_downloads = self.repository.get_all()
        
        for dl in all_downloads:
            if dl.state == DownloadState.DOWNLOADING or dl.state == DownloadState.INITIALIZING:
                # These were interrupted
                dl.state = DownloadState.PAUSED
                self.repository.save(dl)
                
                # Auto-resume
                try:
                    self.download_service.resume_download(dl.id)
                except Exception as e:
                    print(f"Failed to resume {dl.id}: {e}")
