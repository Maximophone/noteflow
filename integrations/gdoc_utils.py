import pickle
import os.path
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import io
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from bs4 import BeautifulSoup
import re
from config.logging_config import setup_logger
import traceback
from config.services_config import GOOGLE_SCOPES

logger = setup_logger(__name__)

class GoogleDocUtils:
    def __init__(self, credentials_path='credentials.json'):
        self.credentials_path = credentials_path
        self.creds = None

    def get_credentials(self):
        if os.path.exists('token.pickle'):
            with open('token.pickle', 'rb') as token:
                self.creds = pickle.load(token)
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, GOOGLE_SCOPES)
                self.creds = flow.run_local_server(port=0)
            with open('token.pickle', 'wb') as token:
                pickle.dump(self.creds, token)
        return self.creds

    @staticmethod
    def extract_doc_id_from_url(url):
        patterns = [
            r'/document/d/([a-zA-Z0-9-_]+)',
            r'/document/u/\d+/d/([a-zA-Z0-9-_]+)',
            r'docs.google.com/.*[?&]id=([a-zA-Z0-9-_]+)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        raise ValueError("Invalid Google Docs URL")

    @staticmethod
    def extract_folder_id_from_url(url):
        patterns = [
            r'/folders/([a-zA-Z0-9-_]+)',
            r'/drive/u/\d+/folders/([a-zA-Z0-9-_]+)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        raise ValueError("Invalid Google Drive folder URL")

    def get_document_as_markdown(self, doc_id_or_url) -> str | None:
        return self.get_document(doc_id_or_url, mime_type='text/markdown')

    def get_document_as_html(self, doc_id_or_url) -> str | None:
        return self.get_document(doc_id_or_url, mime_type='text/html')
    
    def get_document(self, doc_id_or_url, mime_type='text/markdown') -> str | None:
        if 'docs.google.com' in doc_id_or_url:
            doc_id = self.extract_doc_id_from_url(doc_id_or_url)
        else:
            doc_id = doc_id_or_url

        creds = self.get_credentials()
        service = build('drive', 'v3', credentials=creds)

        try:
            request = service.files().export_media(fileId=doc_id, mimeType=mime_type)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
                logger.info(f"Download {int(status.progress() * 100)}%.")

            content = fh.getvalue().decode('utf-8')
            return content

        except Exception as error:
            logger.error(f'An error occurred: {error}')
            logger.error(traceback.format_exc())
            return None

    @staticmethod
    def remove_styles(html_content):
        soup = BeautifulSoup(html_content, 'html.parser')

        for tag in soup.find_all(True):
            if 'style' in tag.attrs:
                del tag['style']

        for style in soup.find_all('style'):
            style.decompose()

        return str(soup)

    def get_clean_html_document(self, doc_id_or_url):
        html_content = self.get_document_as_html(doc_id_or_url)
        if html_content:
            return self.remove_styles(html_content)
        return None

    def create_document_from_text(self, title: str, text_content: str, folder_id: str, mime_type: str = 'text/plain') -> str | None:
        """Creates a new Google Doc with the given title and text content in the specified folder."""
        creds = self.get_credentials()
        service = build('drive', 'v3', credentials=creds)

        try:
            file_metadata = {
                'name': title,
                'mimeType': 'application/vnd.google-apps.document',
                'parents': [folder_id]
            }

            media = MediaIoBaseUpload(
                io.BytesIO(text_content.encode('utf-8')),
                mimetype=mime_type,
                resumable=True
            )

            file = service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, webViewLink',
                supportsAllDrives=True
            ).execute()

            doc_id = file.get('id')
            doc_link = file.get('webViewLink')
            logger.info(f"Successfully created Google Doc: ID='{doc_id}', Link='{doc_link}'")
            return doc_link

        except Exception as error:
            logger.error(f'An error occurred while creating the document: {error}')
            logger.error(traceback.format_exc())
            return None

    def delete_document(self, file_id: str) -> bool:
        """Deletes a file from Google Drive using its ID."""
        creds = self.get_credentials()
        service = build('drive', 'v3', credentials=creds)

        try:
            service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
            logger.info(f"Successfully deleted Google Drive file with ID: {file_id}")
            return True
        except Exception as error:
            logger.error(f'An error occurred while deleting file {file_id}: {error}')
            logger.error(traceback.format_exc())
            return False





