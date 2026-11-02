from io import BytesIO
import base64
import unittest
from unittest.mock import Mock, patch
from PIL import Image
from logos import LogoCache

def png():
    image=Image.new('RGBA',(512,256),(20,30,40,180));stream=BytesIO();image.save(stream,format='PNG');return stream.getvalue()

class LogoTests(unittest.TestCase):
    def test_logo_is_downloaded_once_normalized_and_reused_for_same_company(self):
        response=Mock();response.status_code=200;response.headers={'Content-Type':'image/png'};response.iter_content.return_value=[png()];response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        body={'items':[{'companyId':'fixture','companyLogo':'https://media.licdn.com/logo.png'} for _ in range(3)],'run':{'warnings':[]}}
        with patch('logos.requests.get',return_value=response) as get:
            cache=LogoCache();cache.enrich(body)
            self.assertEqual(get.call_count,1);self.assertFalse(get.call_args.kwargs['allow_redirects'])
        self.assertEqual(len(body['logos']),1)
        with Image.open(BytesIO(base64.b64decode(body['logos'][0]['data']))) as image:
            self.assertLessEqual(image.width,256);self.assertLessEqual(image.height,256);self.assertEqual(image.format,'WEBP')
        self.assertTrue(all(item['companyLogo'].startswith('https://json.acceso.design/jobs/logos/') for item in body['items']))

    def test_untrusted_hosts_are_never_fetched_and_cdn_failures_are_nonfatal(self):
        cache=LogoCache();body={'items':[{'companyId':'evil','companyLogo':'https://127.0.0.1/logo'}],'run':{'warnings':[]}}
        with patch('logos.requests.get') as get:cache.enrich(body);get.assert_not_called()
        self.assertNotIn('logos',body)

    def test_backfill_caches_existing_logos_without_resubmitting_job_fields(self):
        response=Mock();response.status_code=200;response.headers={'Content-Type':'image/png'};response.iter_content.return_value=[png()];response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        cache=LogoCache(backlog=[{'companyId':'fixture','companyLogo':'https://media.licdn.com/logo.png'}]);body={'items':[],'run':{'warnings':[]}}
        with patch('logos.requests.get',return_value=response):cache.enrich(body)
        self.assertEqual(body['items'],[]);self.assertEqual(len(body['logos']),1)

if __name__=='__main__':unittest.main()
