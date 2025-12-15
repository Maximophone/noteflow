import requests
from config.secrets import CODA_API_KEY
import time
import re

_MAX_RETRIES = 2
_RETRY_DELAY = 30

class CodaClient:
    def __init__(self, api_token):
        self.api_token = api_token
        self.headers = {'Authorization': f'Bearer {api_token}'}
        self.base_url = 'https://coda.io/apis/v1'

    def extract_doc_and_page_id(self, url: str) -> tuple[str, str]:
        """Extract doc_id and page_id from a Coda URL."""
        doc_pattern = r'_d([a-zA-Z0-9\-_\.~]{10})'
        page_pattern = r'_su([^#?\s]+)'
        
        doc_match = re.search(doc_pattern, url)
        if not doc_match:
            raise ValueError("Invalid Coda URL: Could not extract document ID")
            
        doc_id = doc_match.group(1)
        
        url_after_doc = url[url.index(doc_id) + len(doc_id):]
        page_match = re.search(page_pattern, url_after_doc)
        shortened_page_id = page_match.group(1) if page_match else None
        
        if shortened_page_id:
            pages_response = self.list_pages(doc_id)
            for page in pages_response['items']:
                if page['id'].endswith(shortened_page_id):
                    return doc_id, page['id']
            raise ValueError(f"No page found with shortened ID: {shortened_page_id}")
        
        return doc_id, None

    def _make_request(self, method, url, max_retries=_MAX_RETRIES, retry_delay=_RETRY_DELAY, **kwargs):
        retries = 0
        while retries < max_retries:
            try:
                response = requests.request(method, url, headers=self.headers, **kwargs)
                response.raise_for_status()
                return response
            except requests.exceptions.HTTPError as e:
                if e.response.status_code in (404, 409) and retries < max_retries - 1:
                    retries += 1
                    time.sleep(retry_delay)
                else:
                    raise e

    def list_docs(self, is_owner=False, query=None, max_retries=_MAX_RETRIES, retry_delay=_RETRY_DELAY):
        params = {'isOwner': is_owner}
        if query:
            params['query'] = query

        url = f'{self.base_url}/docs'
        response = self._make_request('GET', url, params=params, max_retries=max_retries, retry_delay=retry_delay)
        return response.json()['items']

    def get_doc(self, doc_id, max_retries=_MAX_RETRIES, retry_delay=_RETRY_DELAY):
        url = f'{self.base_url}/docs/{doc_id}'
        response = self._make_request('GET', url, max_retries=max_retries, retry_delay=retry_delay)
        return response.json()

    def get_doc_pages(self, doc_id, max_retries=_MAX_RETRIES, retry_delay=_RETRY_DELAY):
        url = f'{self.base_url}/docs/{doc_id}/pages'
        response = self._make_request('GET', url, max_retries=max_retries, retry_delay=retry_delay)
        return response.json()['items']

    def get_page_content(self, doc_id, page_id_or_name, output_format='html', max_retries=_MAX_RETRIES, retry_delay=_RETRY_DELAY):
        export_url = f'{self.base_url}/docs/{doc_id}/pages/{page_id_or_name}/export'
        payload = {'outputFormat': output_format}

        response = requests.post(export_url, headers=self.headers, json=payload)
        response.raise_for_status()
        request_id = response.json()['id']

        retries = 0
        while retries < max_retries:
            status_url = f'{self.base_url}/docs/{doc_id}/pages/{page_id_or_name}/export/{request_id}'
            status_response = self._make_request('GET', status_url, max_retries=max_retries, retry_delay=retry_delay)
            status = status_response.json()['status']

            if status == 'complete':
                download_url = status_response.json()['downloadLink']
                content_response = requests.get(download_url)
                content_response.raise_for_status()
                return content_response.content
            elif status == 'failed':
                raise Exception(f"Content export failed: {status_response.json()['error']}")
            else:
                retries += 1
                time.sleep(retry_delay)

        raise Exception(f"Failed to retrieve page content after {max_retries} retries.")

    def list_pages(self, doc_id: str) -> dict:
        url = f'{self.base_url}/docs/{doc_id}/pages'
        response = self._make_request('GET', url)
        return response.json()

